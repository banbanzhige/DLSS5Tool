"""User-facing DLSS startup guidance shared by preview and export."""
import re

from dlss5tool.i18n import tr
from dlss5tool.updater import RELEASES_URL


def is_runtime_unsupported(error):
    text = str(error or '')
    return bool(re.search(r'\b0xBAD00001\b|(?<![a-z0-9])FeatureNotSupported\b', text, re.I))


def _guidance_key(error):
    text = str(error or '')
    # Prefer a specific NGX result over the wrapper's generic create failure.
    for code, key in (
        ('0xBAD00002', 'message.dlss_platform_error'),
        ('0xBAD0000C', 'message.dlss_out_of_date'),
        ('0xBAD0000D', 'message.dlss_out_of_gpu_memory'),
        ('0xBAD0000F', 'message.dlss_data_path_error'),
    ):
        if re.search(r'\b' + code + r'\b', text, re.I):
            return key
    if is_runtime_unsupported(text):
        return 'message.dlss_runtime_unsupported'
    if re.search(
        r'\bFeature\s+18\s+(?:create|resize)\s+failed\b'
        r'|\bdlssnr_init\s+failed\b'
        r'|\bCreateFeature\(18\)\s*->\s*0xBAD[0-9a-f]{5}\b',
        text, re.I,
    ):
        return 'message.dlss_initialization_failed'
    return None


def runtime_guidance(error):
    key = _guidance_key(error)
    if not key:
        return ''
    message = tr(key)
    if offers_runtime_download(error):
        message += '\n\n' + tr('message.dlss_runtime_recovery', releases_url=RELEASES_URL)
    return message


def offers_runtime_download(error):
    return _guidance_key(error) in {
        'message.dlss_initialization_failed', 'message.dlss_runtime_unsupported',
    }


def format_error(error):
    """Put the recovery steps first and keep the complete original error."""
    text = str(error or '')
    guidance = runtime_guidance(text)
    if not guidance or guidance in text:
        return text
    return guidance + '\n\n' + tr('message.dlss_error_details', error=text)
