"""
Renders a KHQR payload string into a QR code PNG.

Install dependency:
    pip install qrcode[pil] Pillow
"""
from __future__ import annotations

import io

import qrcode
import qrcode.constants


def generate_qr_png(
    payload: str,
    box_size: int = 10,
    border: int = 4,
) -> bytes:
    """
    Render a KHQR payload string into a PNG image and return raw bytes.

    Args:
        payload:   Complete KHQR string from build_khqr_payload().
        box_size:  Pixels per QR module (higher = bigger image).
        border:    Quiet-zone width in modules.  EMVCo requires ≥ 4.

    Returns:
        PNG image as raw bytes.  Callers can:
          - base64-encode and embed as data:image/png;base64,...
          - write directly to a file
          - store as BYTEA / BLOB in the database
    """
    qr = qrcode.QRCode(
        version=None,                                       # auto-size
        error_correction=qrcode.constants.ERROR_CORRECT_M,  # ~15% damage recovery
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
