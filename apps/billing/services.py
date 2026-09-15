from __future__ import annotations

import stripe
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import CreditWallet, CreditLedger, Subscription, Plan, Invoice, Payment
from apps.identity.models import Organization


class CreditService:
    """Service for managing credit wallets and ledger"""

    @staticmethod
    def get_or_create_wallet(organization: Organization) -> CreditWallet:
        wallet, created = CreditWallet.objects.get_or_create(
            organization=organization,
            defaults={
                'balance': Decimal('0'),
                'reserved_balance': Decimal('0'),
                'currency': 'USD',
                'credit_value_usd': Decimal(str(settings.BILLING_CREDIT_VALUE_USD)),
            }
        )
        return wallet

    @staticmethod
    @transaction.atomic
    def reserve_credits(
        user,
        amount: Decimal,
        request_id,
        job_id=None,
        reason='Reservation'
    ) -> CreditLedger:
        org = user.organizations.first()
        if not org:
            raise ValueError('User must belong to an organization')

        wallet = CreditService.get_or_create_wallet(org)

        if wallet.available_balance < amount:
            raise ValueError(f'Insufficient credits. Available: {wallet.available_balance}, Required: {amount}')

        wallet.reserved_balance += amount
        wallet.save(update_fields=['reserved_balance', 'updated_at'])

        ledger = CreditLedger.objects.create(
            wallet=wallet,
            direction=CreditLedger.Direction.DEBIT,
            credits=amount,
            reason=CreditLedger.Reason.USAGE_CHAT,  # Default, will be updated
            description=reason,
            request_id=request_id,
            job_id=job_id,
            idempotency_key=f'reserve_{request_id}',
            balance_after=wallet.available_balance,
        )

        return ledger

    @staticmethod
    @transaction.atomic
    def release_reservation(user, request_id) -> CreditLedger | None:
        org = user.organizations.first()
        if not org:
            return None

        wallet = CreditService.get_or_create_wallet(org)

        # Find the reservation ledger entry
        reservation = CreditLedger.objects.filter(
            wallet=wallet,
            request_id=request_id,
            direction=CreditLedger.Direction.DEBIT,
        ).first()

        if not reservation:
            return None

        wallet.reserved_balance -= reservation.credits
        wallet.save(update_fields=['reserved_balance', 'updated_at'])

        ledger = CreditLedger.objects.create(
            wallet=wallet,
            direction=CreditLedger.Direction.CREDIT,
            credits=reservation.credits,
            reason=CreditLedger.Reason.ADJUSTMENT,
            description=f'Released reservation: {reservation.description}',
            request_id=request_id,
            idempotency_key=f'release_{request_id}',
            balance_after=wallet.available_balance,
        )

        return ledger

    @staticmethod
    @transaction.atomic
    def settle_reservation(user, request_id, actual_amount: Decimal) -> CreditLedger:
        org = user.organizations.first()
        if not org:
            raise ValueError('User must belong to an organization')

        wallet = CreditService.get_or_create_wallet(org)

        # Find the reservation
        reservation = CreditLedger.objects.filter(
            wallet=wallet,
            request_id=request_id,
            direction=CreditLedger.Direction.DEBIT,
        ).first()

        if not reservation:
            raise ValueError('No reservation found for request_id')

        # Calculate difference
        reserved = reservation.credits
        difference = reserved - actual_amount

        # Release unused reservation
        wallet.reserved_balance -= reserved
        wallet.save(update_fields=['reserved_balance', 'updated_at'])

        # Create settlement entry for actual usage
        ledger = CreditLedger.objects.create(
            wallet=wallet,
            direction=CreditLedger.Direction.DEBIT,
            credits=actual_amount,
            reason=CreditLedger.Reason.USAGE_CHAT,
            description=f'Settled usage (reserved: {reserved}, actual: {actual_amount})',
            request_id=request_id,
            idempotency_key=f'settle_{request_id}',
            balance_after=wallet.available_balance,
        )

        # If there was over-reservation, credit back the difference
        if difference > 0:
            CreditLedger.objects.create(
                wallet=wallet,
                direction=CreditLedger.Direction.CREDIT,
                credits=difference,
                reason=CreditLedger.Reason.ADJUSTMENT,
                description=f'Refunded over-reservation: {difference}',
                request_id=request_id,
                idempotency_key=f'refund_{request_id}',
                balance_after=wallet.available_balance + difference,
            )
            wallet.balance += difference
            wallet.save(update_fields=['balance', 'updated_at'])

        return ledger

    @staticmethod
    @transaction.atomic
    def add_credits(wallet: CreditWallet, amount: Decimal, reason: str, request_id=None, metadata=None) -> CreditLedger:
        wallet.balance += amount
        wallet.save(update_fields=['balance', 'updated_at'])

        ledger = CreditLedger.objects.create(
            wallet=wallet,
            direction=CreditLedger.Direction.CREDIT,
            credits=amount,
            reason=CreditLedger.Reason.MANUAL_TOPUP,
            description=reason,
            request_id=request_id,
            idempotency_key=f'topup_{request_id or timezone.now().timestamp()}',
            balance_after=wallet.available_balance,
            metadata=metadata or {},
        )

        return ledger

    @staticmethod
    @transaction.atomic
    def grant_subscription_credits(subscription: Subscription):
        """Grant monthly credits for subscription"""
        wallet = CreditService.get_or_create_wallet(subscription.organization)
        plan = subscription.plan

        amount = plan.monthly_credits
        if amount <= 0:
            return None

        wallet.balance += amount
        wallet.save(update_fields=['balance', 'updated_at'])

        ledger = CreditLedger.objects.create(
            wallet=wallet,
            direction=CreditLedger.Direction.CREDIT,
            credits=amount,
            reason=CreditLedger.Reason.SUBSCRIPTION_GRANT,
            description=f'Monthly credits for {plan.name} plan',
            request_id=subscription.id,
            idempotency_key=f'sub_grant_{subscription.id}_{timezone.now().month}',
            balance_after=wallet.available_balance,
        )

        return ledger


class StripeService:
    """Service for Stripe integration"""

    def __init__(self):
        stripe.api_key = settings.STRIPE_SECRET_KEY

    def create_customer(self, organization: Organization) -> stripe.Customer:
        customer = stripe.Customer.create(
            email=organization.owner.email if organization.owner else '',
            name=organization.name,
            metadata={'organization_id': str(organization.id)},
        )
        return customer

    def get_or_create_customer(self, organization: Organization) -> stripe.Customer:
        # Check if organization already has a customer
        subscriptions = Subscription.objects.filter(organization=organization)
        for sub in subscriptions:
            if sub.provider_customer_id:
                try:
                    return stripe.Customer.retrieve(sub.provider_customer_id)
                except stripe.error.InvalidRequestError:
                    pass

        # Create new customer
        customer = self.create_customer(organization)
        # Update subscriptions with customer ID
        subscriptions.update(provider_customer_id=customer.id)
        return customer

    def create_checkout_session(
        self,
        organization: Organization,
        plan: Plan,
        success_url: str = None,
        cancel_url: str = None
    ) -> str:
        customer = self.get_or_create_customer(organization)

        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': plan.currency.lower(),
                    'unit_amount': plan.price_cents,
                    'recurring': {'interval': plan.interval},
                    'product_data': {
                        'name': plan.name,
                        'description': plan.description,
                    },
                },
                'quantity': 1,
            }],
            mode='subscription',
            success_url=success_url or f'{settings.FRONTEND_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=cancel_url or f'{settings.FRONTEND_URL}/billing/cancel',
            metadata={
                'organization_id': str(organization.id),
                'plan_id': str(plan.id),
            },
            subscription_data={
                'metadata': {
                    'organization_id': str(organization.id),
                    'plan_id': str(plan.id),
                }
            },
        )
        return session.url

    def create_payment_intent(
        self,
        organization: Organization,
        amount_cents: int,
        payment_method_id: str = None,
        currency: str = 'usd'
    ) -> stripe.PaymentIntent:
        customer = self.get_or_create_customer(organization)

        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=currency,
            customer=customer.id,
            payment_method=payment_method_id,
            confirm=payment_method_id is not None,
            metadata={'organization_id': str(organization.id)},
        )
        return intent

    def cancel_subscription(self, subscription_id: str):
        stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True
        )

    def reactivate_subscription(self, subscription_id: str):
        stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=False
        )

    def handle_webhook_event(self, event: stripe.Event):
        """Process Stripe webhook events"""
        event_type = event.type
        data = event.data.object

        if event_type == 'checkout.session.completed':
            self._handle_checkout_completed(data)
        elif event_type == 'invoice.payment_succeeded':
            self._handle_payment_succeeded(data)
        elif event_type == 'invoice.payment_failed':
            self._handle_payment_failed(data)
        elif event_type == 'customer.subscription.updated':
            self._handle_subscription_updated(data)
        elif event_type == 'customer.subscription.deleted':
            self._handle_subscription_deleted(data)
        elif event_type == 'payment_intent.succeeded':
            self._handle_payment_intent_succeeded(data)

    def _handle_checkout_completed(self, session):
        organization_id = session.metadata.get('organization_id')
        plan_id = session.metadata.get('plan_id')

        if not organization_id or not plan_id:
            return

        try:
            org = Organization.objects.get(id=organization_id)
            plan = Plan.objects.get(id=plan_id)
        except (Organization.DoesNotExist, Plan.DoesNotExist):
            return

        subscription = Subscription.objects.create(
            organization=org,
            plan=plan,
            status=Subscription.Status.ACTIVE,
            provider='stripe',
            provider_subscription_id=session.subscription,
            provider_customer_id=session.customer,
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timezone.timedelta(days=30),
        )

        # Grant initial credits
        CreditService.grant_subscription_credits(subscription)

    def _handle_payment_succeeded(self, invoice):
        subscription_id = invoice.subscription
        if not subscription_id:
            return

        try:
            sub = Subscription.objects.get(provider_subscription_id=subscription_id)
        except Subscription.DoesNotExist:
            return

        # Update period
        sub.current_period_start = timezone.datetime.fromtimestamp(invoice.period_start, tz=timezone.utc)
        sub.current_period_end = timezone.datetime.fromtimestamp(invoice.period_end, tz=timezone.utc)
        sub.status = Subscription.Status.ACTIVE
        sub.save(update_fields=['current_period_start', 'current_period_end', 'status'])

        # Create invoice record
        Invoice.objects.get_or_create(
            provider_invoice_id=invoice.id,
            defaults={
                'organization': sub.organization,
                'subscription': sub,
                'provider': 'stripe',
                'status': Invoice.Status.PAID,
                'amount_cents': invoice.amount_paid,
                'currency': invoice.currency.upper(),
                'period_start': sub.current_period_start,
                'period_end': sub.current_period_end,
                'paid_at': timezone.now(),
            }
        )

        # Grant monthly credits
        CreditService.grant_subscription_credits(sub)

    def _handle_payment_failed(self, invoice):
        subscription_id = invoice.subscription
        if not subscription_id:
            return

        try:
            sub = Subscription.objects.get(provider_subscription_id=subscription_id)
        except Subscription.DoesNotExist:
            return

        sub.status = Subscription.Status.PAST_DUE
        sub.save(update_fields=['status'])

    def _handle_subscription_updated(self, subscription):
        try:
            sub = Subscription.objects.get(provider_subscription_id=subscription.id)
        except Subscription.DoesNotExist:
            return

        status_map = {
            'active': Subscription.Status.ACTIVE,
            'trialing': Subscription.Status.TRIALING,
            'past_due': Subscription.Status.PAST_DUE,
            'canceled': Subscription.Status.CANCELED,
            'incomplete': Subscription.Status.INCOMPLETE,
            'paused': Subscription.Status.PAUSED,
        }

        new_status = status_map.get(subscription.status, Subscription.Status.INCOMPLETE)
        sub.status = new_status
        sub.cancel_at_period_end = subscription.cancel_at_period_end
        sub.current_period_start = timezone.datetime.fromtimestamp(subscription.current_period_start, tz=timezone.utc)
        sub.current_period_end = timezone.datetime.fromtimestamp(subscription.current_period_end, tz=timezone.utc)

        if subscription.canceled_at:
            sub.canceled_at = timezone.datetime.fromtimestamp(subscription.canceled_at, tz=timezone.utc)

        sub.save()

    def _handle_subscription_deleted(self, subscription):
        try:
            sub = Subscription.objects.get(provider_subscription_id=subscription.id)
        except Subscription.DoesNotExist:
            return

        sub.status = Subscription.Status.CANCELED
        sub.canceled_at = timezone.now()
        sub.save(update_fields=['status', 'canceled_at'])

    def _handle_payment_intent_succeeded(self, intent):
        organization_id = intent.metadata.get('organization_id')
        if not organization_id:
            return

        try:
            org = Organization.objects.get(id=organization_id)
        except Organization.DoesNotExist:
            return

        wallet = CreditService.get_or_create_wallet(org)
        credits = Decimal(str(intent.amount)) / Decimal('100') / Decimal(str(settings.BILLING_CREDIT_VALUE_USD))

        CreditService.add_credits(
            wallet=wallet,
            amount=credits,
            reason=f'Top-up via Stripe: ${intent.amount/100:.2f}',
            request_id=intent.id,
            metadata={'stripe_payment_intent_id': intent.id}
        )

        # Create payment record
        Payment.objects.create(
            organization=org,
            wallet=wallet,
            provider='stripe',
            provider_payment_id=intent.id,
            type=Payment.Type.TOPUP,
            status=Payment.Status.SUCCEEDED,
            amount_cents=intent.amount,
            currency=intent.currency.upper(),
            credits_granted=credits,
            idempotency_key=f'topup_{intent.id}',
            succeeded_at=timezone.now(),
        )