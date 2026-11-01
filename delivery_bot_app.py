"""Compatibility shim for the Cloudflare-hosted manual-delivery bot.

The delivery bot now uses Telegram webhooks on the Cloudflare API Worker.
The token must not be loaded by the Render process. Imports remain available
for older modules during rollout, but no polling bot is created here.
"""

delivery_bot = None
delivery_dp = None
