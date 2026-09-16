from fastapi import Request


def set_flash(request: Request, message: str, kind: str = "info"):
    request.session["flash"] = {"message": message, "kind": kind}


def pop_flash(request: Request):
    return request.session.pop("flash", None)
