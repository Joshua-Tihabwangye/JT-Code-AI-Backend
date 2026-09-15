from __future__ import annotations

import json
from confluent_kafka import Consumer, KafkaError
from django.conf import settings
from django.core.management.base import BaseCommand
import sentry_sdk

class Command(BaseCommand):
    help = 'Run the JT-Code Kafka consumer skeleton.'

    def add_arguments(self, parser):
        parser.add_argument('topics', nargs='+')
        parser.add_argument('--group-id', default='jt-code-api-consumer')

    def handle(self, *args, **options):
        topics = [topic if topic.startswith(settings.KAFKA_TOPIC_PREFIX) else f'{settings.KAFKA_TOPIC_PREFIX}.{topic}' for topic in options['topics']]
        consumer = Consumer({
            'bootstrap.servers': settings.KAFKA_BOOTSTRAP_SERVERS,
            'group.id': options['group_id'],
            'auto.offset.reset': 'earliest',
            'enable.auto.commit': False,
            'security.protocol': settings.KAFKA_SECURITY_PROTOCOL,
        })
        consumer.subscribe(topics)
        self.stdout.write(f'Consuming {topics}')
        try:
            while True:
                message = consumer.poll(1.0)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() != KafkaError._PARTITION_EOF:
                        raise RuntimeError(str(message.error()))
                    continue
                try:
                    payload = json.loads(message.value())
                    self.stdout.write(f"{message.topic()} {payload.get('requestId', '')}")
                    # Route to an idempotent domain handler here.
                    consumer.commit(message=message, asynchronous=False)
                except Exception as exc:
                    sentry_sdk.capture_exception(exc)
                    raise
        finally:
            consumer.close()
