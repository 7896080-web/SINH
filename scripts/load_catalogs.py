"""One-off: load marketplace catalogs and create sync proposals (the lightning
bolt on the Sync Products page) for barcodes found in each cabinet. Same work
job_catalog_poll does every 24h. Run: .venv\\Scripts\\python.exe load_catalogs.py
"""
from app.database import SessionLocal
from app.models import PlatformAccount
from app.workers.client_factory import build_client
from app.workers.catalog_sync import load_platform_catalog
from app.workers.catalog_poller import poll_catalog
from app.workers.credentials import CredentialsMissing


def main():
    s = SessionLocal()
    accs = s.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all()
    for a in accs:
        try:
            c = build_client(s, a.id)
            load_stats = load_platform_catalog(s, c, a)
            prop_stats = poll_catalog(s, a)
            print("[%s] %s  load=%s  proposals=%s" % (a.platform.value, a.name, load_stats, prop_stats))
        except CredentialsMissing as e:
            print("[%s] %s  NO CREDENTIALS: %s" % (a.platform.value, a.name, e))
        except Exception as e:
            print("[%s] %s  ERROR: %s" % (a.platform.value, a.name, e))
    s.close()


if __name__ == "__main__":
    main()
