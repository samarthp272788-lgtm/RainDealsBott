Place the deposit QR image files in this folder using these exact names:
- upi.png
- bep20.png
- polygon.png

Then configure these Render environment variables (adjust the path if the Render service root differs):
UPI_QR_FILE=/opt/render/project/src/assets/qr/upi.png
BEP20_QR_FILE=/opt/render/project/src/assets/qr/bep20.png
POLYGON_QR_FILE=/opt/render/project/src/assets/qr/polygon.png

Commit and push the image files so Render includes them in the deployment.
