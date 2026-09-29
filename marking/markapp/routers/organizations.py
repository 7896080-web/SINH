from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from markapp import audit, settings
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import Organization, User
from markapp.pages import render

router = APIRouter()

FIELDS = ("name", "surname", "firstname", "patronymic", "inn", "ogrnip", "address",
          "signer_role", "contract_number", "sticker_sender", "edo_sender_id")


@router.get("/organizations")
def org_list(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    orgs = db.query(Organization).order_by(Organization.id).all()
    lamoda = settings.lamoda_org(db)
    return render(request, "organizations.html", user, "organizations", orgs=orgs,
                  lamoda_id=lamoda.id if lamoda else None,
                  agency_from=settings.get(db, settings.AGENCY_FROM),
                  last_number=settings.get(db, settings.SUPPLY_LAST_NUMBER),
                  step=settings.get(db, settings.SUPPLY_STEP))


@router.get("/organizations/{org_id}")
def org_edit(org_id: int, request: Request, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    org = db.get(Organization, org_id) if org_id else Organization(vat_rate=5, signer_role="ИП",
                                                                     contract_number="б/н")
    if org is None:
        return RedirectResponse("/organizations", status_code=303)
    return render(request, "organization.html", user, "organizations", org=org, fields=FIELDS)


@router.post("/organizations/{org_id}")
def org_save(org_id: int, request: Request, db: Session = Depends(get_db),
             user: User = Depends(get_current_user),
             name: str = Form(...), surname: str = Form(...), firstname: str = Form(...),
             patronymic: str = Form(""), inn: str = Form(...), ogrnip: str = Form(...),
             address: str = Form(...), signer_role: str = Form("ИП"), vat_rate: int = Form(5),
             contract_number: str = Form("б/н"), sticker_sender: str = Form(""),
             edo_sender_id: str = Form("")):
    inn = inn.strip()
    if not (inn.isdigit() and len(inn) in (10, 12)):
        flash(request, "ИНН — 10 или 12 цифр.", "error")
        return RedirectResponse(f"/organizations/{org_id}", status_code=303)
    org = db.get(Organization, org_id) if org_id else None
    if org is None:
        org = Organization()
        db.add(org)
    values = dict(name=name, surname=surname, firstname=firstname, patronymic=patronymic,
                  inn=inn, ogrnip=ogrnip, address=address, signer_role=signer_role,
                  contract_number=contract_number, sticker_sender=sticker_sender,
                  edo_sender_id=edo_sender_id)
    for k, v in values.items():
        setattr(org, k, v.strip())
    org.vat_rate = vat_rate
    try:
        db.flush()
    except Exception:
        db.rollback()
        flash(request, "Организация с таким ИНН уже есть.", "error")
        return RedirectResponse("/organizations", status_code=303)
    audit.log(db, user.username, "organization_saved", org.name, f"ИНН {org.inn}")
    db.commit()
    flash(request, f"Сохранено: {org.name}.", "ok")
    return RedirectResponse("/organizations", status_code=303)


@router.post("/lamoda-settings")
def org_lamoda(request: Request, org_id: int = Form(...), agency_from: str = Form(...),
               last_number: str = Form(...), step: str = Form(...),
               db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org = db.get(Organization, org_id)
    try:
        from markapp.timeutils import parse_ru
        parse_ru(agency_from)
        if not (last_number.strip().isdigit() and step.strip().isdigit() and int(step) > 0):
            raise ValueError
    except ValueError:
        flash(request, "Дата — ДД.ММ.ГГГГ, номер и шаг — целые числа.", "error")
        return RedirectResponse("/organizations", status_code=303)
    if org is None:
        flash(request, "Организация не найдена.", "error")
        return RedirectResponse("/organizations", status_code=303)
    before = settings.lamoda_org(db)
    settings.put(db, settings.LAMODA_ORG, str(org.id))
    settings.put(db, settings.AGENCY_FROM, agency_from.strip())
    settings.put(db, settings.SUPPLY_LAST_NUMBER, last_number.strip())
    settings.put(db, settings.SUPPLY_STEP, step.strip())
    audit.log(db, user.username, "lamoda_settings",
              details=f"ИП для Lamoda: {before.name if before else '—'} → {org.name}; "
                      f"агентский с {agency_from}; последний номер {last_number}, шаг {step}")
    db.commit()
    flash(request, "Настройки Lamoda сохранены. Созданные поставки остаются за своим ИП.", "ok")
    return RedirectResponse("/organizations", status_code=303)
