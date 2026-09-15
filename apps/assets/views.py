from __future__ import annotations

import time
import cloudinary.api
import cloudinary.utils
from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.assets.models import Asset
from apps.assets.serializers import AssetSerializer, CompleteUploadSerializer, SignatureRequestSerializer
from apps.events.outbox import add_outbox_event

class AssetListView(ListAPIView):
    serializer_class = AssetSerializer
    def get_queryset(self):
        return Asset.objects.filter(owner=self.request.user).exclude(status=Asset.Status.DELETED).order_by('-created_at')

class CloudinarySignatureView(APIView):
    def post(self, request: Request) -> Response:
        serializer = SignatureRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data['bytes'] > settings.CLOUDINARY_MAX_UPLOAD_BYTES:
            return Response({'detail': 'File exceeds the configured upload limit.'}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        if not all((settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET)):
            return Response({'detail': 'Cloudinary is not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        timestamp = int(time.time())
        folder = f"{settings.CLOUDINARY_UPLOAD_FOLDER}/{request.user.id}"
        params = {'timestamp': timestamp, 'folder': folder}
        signature = cloudinary.utils.api_sign_request(params, settings.CLOUDINARY_API_SECRET)
        return Response({
            'cloudName': settings.CLOUDINARY_CLOUD_NAME,
            'apiKey': settings.CLOUDINARY_API_KEY,
            'timestamp': timestamp,
            'signature': signature,
            'folder': folder,
            'uploadUrl': f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/auto/upload",
        })

class CompleteUploadView(APIView):
    def post(self, request: Request) -> Response:
        serializer = CompleteUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        public_id = serializer.validated_data['publicId']
        expected_prefix = f"{settings.CLOUDINARY_UPLOAD_FOLDER}/{request.user.id}/"
        if not public_id.startswith(expected_prefix):
            return Response({'detail': 'Uploaded asset is outside the authorized folder.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            resource = cloudinary.api.resource(public_id, resource_type=serializer.validated_data['resourceType'])
        except Exception:
            return Response({'detail': 'Cloudinary asset could not be verified.'}, status=status.HTTP_400_BAD_REQUEST)
        if int(resource.get('bytes', -1)) != serializer.validated_data['bytes']:
            return Response({'detail': 'Cloudinary asset size mismatch.'}, status=status.HTTP_409_CONFLICT)
        try:
            with transaction.atomic():
                asset = Asset.objects.create(
                    owner=request.user,
                    cloudinary_public_id=public_id,
                    secure_url=resource.get('secure_url') or serializer.validated_data['secureUrl'],
                    resource_type=resource.get('resource_type') or serializer.validated_data['resourceType'],
                    format=resource.get('format') or serializer.validated_data.get('format', ''),
                    bytes=resource.get('bytes') or serializer.validated_data['bytes'],
                    version=resource.get('version') or serializer.validated_data['version'],
                    original_filename=serializer.validated_data['originalFilename'],
                    metadata={'etag': resource.get('etag'), 'asset_id': resource.get('asset_id')},
                )
                add_outbox_event('asset.created', str(asset.id), {
                    'assetId': str(asset.id), 'publicId': public_id, 'ownerId': str(request.user.id),
                    'resourceType': asset.resource_type, 'bytes': asset.bytes,
                })
        except IntegrityError:
            asset = Asset.objects.get(cloudinary_public_id=public_id, owner=request.user)
        return Response(AssetSerializer(asset).data, status=status.HTTP_201_CREATED)
