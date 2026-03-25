# -*- coding: utf-8 -*-
"""Upload images to Cloudflare R2 using S3-compatible API with AWS SigV4 signing."""
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

import requests

_logger = logging.getLogger(__name__)

R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '057f90972beedaab0922d33c3ff98b51')
R2_BUCKET = os.environ.get('R2_BUCKET', 'mintdeals-cdn')
R2_PUBLIC_URL = os.environ.get(
    'R2_PUBLIC_URL',
    'https://pub-555ea67ef1fb4921a864c8f34b530a51.r2.dev'
)
R2_REGION = 'auto'


def _sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _get_signature_key(secret_key, date_stamp):
    k_date = _sign(('AWS4' + secret_key).encode('utf-8'), date_stamp)
    k_region = _sign(k_date, R2_REGION)
    k_service = _sign(k_region, 's3')
    return _sign(k_service, 'aws4_request')


def upload_to_r2(binary_data, key, content_type):
    """Upload bytes to R2 and return the public URL.

    Args:
        binary_data: Raw image bytes
        key: Object key (e.g. "stores/tempe/hero.png")
        content_type: MIME type (e.g. "image/png")

    Returns:
        Public URL string

    Raises:
        ValueError: If credentials are missing
        requests.HTTPError: If upload fails
    """
    access_key = os.environ.get('R2_ACCESS_KEY_ID')
    secret_key = os.environ.get('R2_SECRET_ACCESS_KEY')
    if not access_key or not secret_key:
        raise ValueError("R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY env vars required")

    host = f"{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    url = f"https://{host}/{R2_BUCKET}/{key}"

    now = datetime.now(timezone.utc)
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = now.strftime('%Y%m%d')

    payload_hash = hashlib.sha256(binary_data).hexdigest()

    headers_to_sign = {
        'content-type': content_type,
        'host': host,
        'x-amz-content-sha256': payload_hash,
        'x-amz-date': amz_date,
    }

    signed_headers = ';'.join(sorted(headers_to_sign))
    canonical_headers = ''.join(
        f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items())
    )

    canonical_request = '\n'.join([
        'PUT',
        f'/{R2_BUCKET}/{key}',
        '',
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{R2_REGION}/s3/aws4_request"
    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256',
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
    ])

    signing_key = _get_signature_key(secret_key, date_stamp)
    signature = hmac.new(
        signing_key, string_to_sign.encode('utf-8'), hashlib.sha256
    ).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    resp = requests.put(url, data=binary_data, headers={
        'Authorization': auth_header,
        'Content-Type': content_type,
        'Cache-Control': 'public, max-age=2592000',
        'x-amz-content-sha256': payload_hash,
        'x-amz-date': amz_date,
    }, timeout=30)

    resp.raise_for_status()

    public_url = f"{R2_PUBLIC_URL}/{key}"
    _logger.info("Uploaded to R2: %s (%d bytes)", public_url, len(binary_data))
    return public_url
