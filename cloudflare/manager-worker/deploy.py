"""Deploy the database manager without displaying local secrets."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values

def main():
    values = {**read_env(ROOT / ".env"), **read_env(ROOT / "manager_web" / ".env")}
    secrets = {k: values.get(k, "") for k in ("MANAGER_USERNAME", "MANAGER_PASSWORD")}
    if not all(secrets.values()):
        raise SystemExit("Add MANAGER_USERNAME and MANAGER_PASSWORD to manager_web/.env first.")
    if len(secrets["MANAGER_PASSWORD"]) < 12:
        raise SystemExit("MANAGER_PASSWORD must contain at least 12 characters.")
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise SystemExit("Node/npm is required for deployment.")
    with tempfile.TemporaryDirectory(prefix="rd-manager-") as temporary:
        secret_file = Path(temporary) / "secrets.json"
        secret_file.write_text(json.dumps(secrets), encoding="utf-8")
        subprocess.run([npx, "wrangler", "deploy", "--config", str(HERE / "wrangler.jsonc"), "--secrets-file", str(secret_file)], cwd=HERE, check=True)
    print("Manager deployed. No bot configuration, webhook, or Render service was changed.")

if __name__ == "__main__":
    main()
