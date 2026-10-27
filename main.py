from __future__ import annotations

import asyncio
import base64
import gc
import html
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from playwright.async_api import BrowserContext, Page, async_playwright
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")

# ---------- Configuration ----------
def env_int(name: str, default: int) -> int:
    """Lit un entier depuis l'environnement ; vide ou invalide -> valeur par défaut."""
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_CHAT_ID = env_int("ALLOWED_CHAT_ID", 0)  # obligatoire pour utiliser /lab

SKILLS_BASE = os.getenv("SKILLS_BASE", "https://www.skills.google")
SKILLS_EMAIL = os.getenv("SKILLS_EMAIL", "")
SKILLS_PASSWORD = os.getenv("SKILLS_PASSWORD", "")
LAB_URL = os.getenv("LAB_URL", "")  # URL de la page du lab

# Session Google enregistrée (cookies) : si elle est présente, la connexion Google est entièrement sautée.
# Cherchée dans : STORAGE_STATE_PATH, ./storage_state.json, /etc/secrets/storage_state.json (Secret File Render),
# puis dans la variable STORAGE_STATE_B64 (JSON encodé en base64).
STORAGE_STATE_PATHS = [
    p for p in (os.getenv("STORAGE_STATE_PATH", "").strip(), "storage_state.json", "/etc/secrets/storage_state.json") if p
]
STORAGE_STATE_B64 = os.getenv("STORAGE_STATE_B64", "").strip()

# 1 = confirme automatiquement la boîte de dialogue "Start Lab" (peut consommer des crédits !)
AUTO_CONFIRM_DIALOG = os.getenv("AUTO_CONFIRM_DIALOG", "0") == "1"
# 1 = bloque images / médias / polices pour économiser la RAM
BLOCK_HEAVY_RESOURCES = os.getenv("BLOCK_HEAVY_RESOURCES", "1") == "1"

TIMEOUT_MS = env_int("TIMEOUT_MS", 45000)
LAB_START_TIMEOUT_MS = env_int("LAB_START_TIMEOUT_MS", 240000)
RUN_SEARCH_TIMEOUT_S = env_int("RUN_SEARCH_TIMEOUT_S", 60)

CONSOLE_RUN_URL = os.getenv("CONSOLE_RUN_URL", "https://console.cloud.google.com/run")

# ---------- Paramètres FIXES du déploiement Cloud Run (ne pas lire depuis l'environnement) ----------
CR_COUNTRY = "Angola"                             # Country (conditions d'utilisation)
CR_IMAGE_URL = "docker.io/ajndjd2/ahmed-vip1"   # Container image URL
CR_SERVICE_NAME = "ahmed-vip1"                    # Service name
CR_AUTH_PATTERN = r"allow public access"          # Authentication
CR_BILLING_PATTERN = r"instance-based"            # Billing
CR_INGRESS_PATTERN = r"^\s*all\b"                 # Ingress : "All" (et non "Allow...")
TERMS_WAIT_S = env_int("TERMS_WAIT_S", 20)          # attente de la boîte des conditions
DEPLOY_TIMEOUT_S = env_int("DEPLOY_TIMEOUT_S", 240)  # attente de l'URL après "Create"
REGION_RETRIES = 4                                   # essais « Retry » si les régions ne se chargent pas
REGION_RETRY_WAIT_S = env_int("REGION_RETRY_WAIT_S", 15)
API_PROPAGATION_S = env_int("API_PROPAGATION_S", 30)  # attente après activation de l'API Cloud Run Admin
API_LIBRARY_URL = os.getenv(
    "API_LIBRARY_URL", "https://console.cloud.google.com/apis/library/run.googleapis.com"
)
REGION_ERROR_RE = re.compile(
    r"regions can.t be retrieved|Cloud Run Admin API has not been used", re.I
)

RUN_APP_RE = re.compile(r"https://[a-z0-9][a-z0-9.\-]*\.run\.app")
PROJECT_RE = re.compile(r"qwiklabs-gcp-[\w\-]+")
USER_RE = re.compile(r"[\w.\-]+@qwiklabs\.net")
OPEN_CONSOLE_RE = re.compile(r"open google (cloud )?console", re.I)

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
JS_MAX_HEAP_MB = env_int("JS_MAX_HEAP_MB", 256)  # plafond mémoire JS de Chromium

# Options Chromium orientées faible consommation mémoire
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--mute-audio",
    "--renderer-process-limit=1",
    f"--js-flags=--max-old-space-size={JS_MAX_HEAP_MB}",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HIDE_WEBDRIVER_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"

# Un seul lab à la fois (un seul Chromium en mémoire)
LAB_LOCK = asyncio.Lock()


GOOGLE_ACCOUNT_HOST_RE = re.compile(r"(^|\.)(google\.com|youtube\.com)$", re.I)


def strip_google_account_state(state: dict) -> tuple[dict, int]:
    """Retire cookies / localStorage de google.com et youtube.com (compte Google PERSONNEL).

    Sinon la console Cloud s'ouvrirait avec ton compte perso au lieu du compte du lab (qwiklabs).
    La session de Google Skills (domaines *.skills.google, *.cloudskillsboost.google) est conservée.
    """
    cookies = state.get("cookies", [])
    kept = [c for c in cookies if not GOOGLE_ACCOUNT_HOST_RE.search((c.get("domain") or "").lstrip("."))]
    origins = [
        o for o in state.get("origins", [])
        if not re.search(r"://([^/]*\.)?(google\.com|youtube\.com)(:\d+)?(/|$)", o.get("origin", ""), re.I)
    ]
    return {**state, "cookies": kept, "origins": origins}, len(cookies) - len(kept)


def convert_browser_cookie(c: dict) -> dict | None:
    """Cookie exporté par une extension (Cookie-Editor...) -> cookie Playwright."""
    if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
        return None
    ss = c.get("sameSite")  # Firefox exporte null pour "non spécifié" -> Lax
    same = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}.get(
        str(ss).lower() if ss else "", "Lax"
    )
    exp = c.get("expirationDate", c.get("expires", -1))
    out = {
        "name": c["name"],
        "value": c.get("value", ""),
        "domain": c["domain"],
        "path": c.get("path") or "/",
        "expires": float(exp) if isinstance(exp, (int, float)) and not c.get("session") else -1,
        "httpOnly": bool(c.get("httpOnly", False)),
        "secure": bool(c.get("secure", False)) or same == "None",
        "sameSite": same,
    }
    return out


def load_storage_state() -> dict | None:
    """Session enregistrée (dict prêt pour new_context), ou None si absente / illisible / vide."""
    raw = None
    for path in STORAGE_STATE_PATHS:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                logger.info("Session enregistrée trouvée : %s", path)
                break
            except Exception as exc:
                logger.warning("Fichier de session %s illisible (%s) : ignoré.", path, type(exc).__name__)
    if raw is None and STORAGE_STATE_B64:
        try:
            raw = json.loads(base64.b64decode(STORAGE_STATE_B64).decode("utf-8"))
            logger.info("Session enregistrée chargée depuis STORAGE_STATE_B64.")
        except Exception as exc:
            logger.warning("STORAGE_STATE_B64 invalide (%s) : ignoré.", type(exc).__name__)
    if isinstance(raw, list):  # export Cookie-Editor / EditThisCookie (liste de cookies) -> format Playwright
        raw = {"cookies": [c for c in (convert_browser_cookie(x) for x in raw) if c], "origins": []}
    if not isinstance(raw, dict):
        return None
    state, removed = strip_google_account_state(raw)
    if removed:
        logger.info("Session : %d cookie(s) du compte Google personnel retirés (la console utilisera le compte du lab).", removed)
    if not state["cookies"]:
        logger.warning("Session enregistrée sans cookie Skills utilisable : ignorée (connexion normale).")
        return None
    return state


def redact(text: str, secrets: list[str]) -> str:
    """Remplace chaque secret (mots de passe) par *** avant d'afficher ou de journaliser une erreur."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class LabError(Exception):
    def __init__(self, message: str, screenshot: bytes | None = None):
        super().__init__(message)
        self.screenshot = screenshot


# ---------- Gestion des pages / de la mémoire ----------
async def safe_close(page: Page | None) -> None:
    """Ferme une page sans jamais lever d'exception."""
    try:
        if page is not None and not page.is_closed():
            await page.close()
    except Exception:
        pass


async def enforce_single_tab(context: BrowserContext, keep: Page | None) -> None:
    """Ferme tous les onglets sauf `keep` (ou tous si keep est None)."""
    for pg in list(context.pages):
        if pg is not keep:
            await safe_close(pg)


def last_open_page(context: BrowserContext) -> Page | None:
    pages = [p for p in context.pages if not p.is_closed()]
    return pages[-1] if pages else None


async def block_heavy_resources(context: BrowserContext) -> None:
    async def handler(route):
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handler)


# ---------- Utilitaires Playwright ----------
async def click_if_present(page: Page, pattern: str, timeout: int = 3000) -> bool:
    """Clique sur un bouton s'il apparaît rapidement (pop-ups optionnels)."""
    try:
        await page.get_by_role("button", name=re.compile(pattern, re.I)).first.click(timeout=timeout)
        return True
    except Exception:
        return False


async def find_run_urls(page: Page, timeout_s: int, prefix: str | None = None) -> list[str]:
    """Cherche des URLs *.run.app dans la page et ses iframes (polling)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found: set[str] = set()
        for frame in page.frames:
            try:
                found |= set(RUN_APP_RE.findall(await frame.content()))
            except Exception:
                pass
        found = {u for u in found if not u.endswith(".region.run.app")}  # URL fictive du formulaire
        if prefix:
            found = {u for u in found if u.startswith(prefix)}
        if found:
            return sorted(found)
        await page.wait_for_timeout(3000)
    return []


# ---------- Étapes du flux ----------
# ---------- Détection de session (connecté / déconnecté) ----------
SIGN_IN_RE = re.compile(r"^\s*sign in\s*$", re.I)  # nom EXACT : exclut « Sign in with email and password »
LOGIN_ERROR_RE = re.compile(
    r"invalid|incorrect|wrong|not recognized|couldn.?t|could not|try again|doesn.?t match|locked|too many",
    re.I,
)


async def snapshot(page: Page) -> bytes | None:
    """Capture PNG pour le diagnostic (ne lève jamais d'exception)."""
    try:
        return await page.screenshot(type="png")
    except Exception:
        return None


async def is_vis(loc) -> bool:
    try:
        return await loc.is_visible()
    except Exception:
        return False


def sign_in_controls(page: Page):
    """Lien OU bouton « Sign in » : l'en-tête d'un visiteur non connecté."""
    return page.get_by_role("link", name=SIGN_IN_RE).or_(page.get_by_role("button", name=SIGN_IN_RE))


async def is_signed_out(page: Page) -> bool:
    """True si un « Sign in » est visible (on teste tous les éléments : un doublon mobile peut être caché)."""
    try:
        for el in await sign_in_controls(page).all():
            if await el.is_visible():
                return True
    except Exception:
        pass
    return False


async def login_error_text(page: Page) -> str:
    """Texte d'un message d'erreur de connexion visible (identifiants refusés…), sinon ''."""
    try:
        for el in await page.locator("[role='alert'], [class*='error' i], [class*='alert' i]").all():
            if await el.is_visible():
                txt = " ".join((await el.inner_text()).split())
                if txt and len(txt) <= 200 and LOGIN_ERROR_RE.search(txt):
                    return redact(txt, [SKILLS_PASSWORD])
    except Exception:
        pass
    return ""


async def confirm_login(page: Page) -> None:
    """Attend que la connexion soit réellement effective ; sinon LabError (avec capture).

    Succès = plus de champ mot de passe ET plus de « Sign in » visible, de façon stable pendant 3 s
    (évite les faux positifs pendant une redirection).
    """
    deadline = time.monotonic() + TIMEOUT_MS / 1000
    stable = 0
    while time.monotonic() < deadline:
        err = await login_error_text(page)
        if err:
            raise LabError(
                f"Google Skills a refusé la connexion : « {err} ». Vérifie SKILLS_EMAIL / SKILLS_PASSWORD.",
                await snapshot(page),
            )
        try:
            form_gone = await page.locator("input[type='password']:visible").count() == 0
            if form_gone and not await is_signed_out(page):
                stable += 1
                if stable >= 3:
                    return
            else:
                stable = 0
        except Exception:
            stable = 0  # navigation en cours
        await page.wait_for_timeout(1000)
    raise LabError(
        "Connexion non confirmée : le formulaire ou le bouton « Sign in » est toujours affiché.",
        await snapshot(page),
    )


# ---------- Connexion à Google Skills via le bouton « Sign in with Google » ----------
GOOGLE_AUTH_TIMEOUT_S = 90
GOOGLE_AUTH_URL_RE = re.compile(r"^https://accounts\.(google|youtube)\.com/", re.I)
# Nom accessible du bouton : « Sign in with Google », « Continue with Google », « Se connecter avec Google »…
GOOGLE_BTN_RE = re.compile(
    r"\b(sign|log)[\s\-]*in\b.{0,25}\bgoogle\b|\bgoogle\b.{0,25}\b(sign|log)[\s\-]*in\b"
    r"|\b(continue|continuer|se connecter|connexion)\b.{0,25}\bgoogle\b",
    re.I,
)
# Liens à NE PAS prendre pour le bouton (logo « Google Skills », produits Google…)
GOOGLE_PRODUCT_RE = re.compile(
    r"google\s+(skills|cloud|workspace|play|developers?|for\b|news|maps|chrome|drive|scholar)", re.I
)
# Écrans de consentement / d'information à valider APRÈS la saisie des identifiants
GOOGLE_CONTINUE_RE = re.compile(
    r"^\s*(continue|continuer|allow|autoriser|i understand|j.?ai compris|i agree|j.?accepte|"
    r"accept|accepter|not now|pas maintenant|skip|ignorer)\s*$",
    re.I,
)
GOOGLE_CREDS_RE = re.compile(
    r"wrong password|mot de passe incorrect|couldn.?t find your google account|"
    r"impossible de trouver votre compte google",
    re.I,
)
GOOGLE_BLOCK_RE = re.compile(
    r"this browser or app may not be secure|couldn.?t sign you in|verify it.?s you|2-step verification|"
    r"two-step verification|check your phone|get a verification code|enter the code|captcha|"
    r"type the text you hear or see|confirm your recovery|unusual activity|too many failed attempts|"
    r"ce navigateur ou cette application peut ne pas|vérifiez votre identité|validation en deux étapes",
    re.I,
)


def google_problem(text: str) -> str | None:
    """Message d'erreur si l'écran Google refuse les identifiants ou bloque l'automatisation, sinon None."""
    m = GOOGLE_CREDS_RE.search(text)
    if m:
        return (
            f"Google refuse les identifiants (« {m.group(0)} »). Vérifie SKILLS_EMAIL / SKILLS_PASSWORD : "
            "ce doivent être l'e-mail et le mot de passe du compte Google."
        )
    m = GOOGLE_BLOCK_RE.search(text)
    if m:
        return (
            f"Google exige une vérification que le bot ne peut pas effectuer (« {m.group(0)} » : double "
            "authentification, captcha ou navigateur jugé non sécurisé)."
        )
    return None


async def find_google_button(page: Page):
    """Bouton/lien « Sign in with Google » visible (page principale ou iframe), sinon None."""
    for f in page.frames:  # 1) nom explicite du type « Sign in with Google »
        for role in ("button", "link"):
            try:
                for el in await f.get_by_role(role, name=GOOGLE_BTN_RE).all():
                    if await el.is_visible():
                        return el
            except Exception:
                pass
    for f in page.frames:  # 2) repli : tout bouton/lien contenant « Google » (hors logo / produits Google)
        for role in ("button", "link"):
            try:
                for el in await f.get_by_role(role, name=re.compile(r"google", re.I)).all():
                    if not await el.is_visible():
                        continue
                    name = " ".join(((await el.get_attribute("aria-label")) or (await el.inner_text()) or "").split())
                    if not GOOGLE_PRODUCT_RE.search(name):
                        return el
            except Exception:
                pass
    return None


async def wait_for_google_page(context: BrowserContext, timeout_s: int = 20) -> Page | None:
    """Page Google d'authentification : popup OU même onglet redirigé vers accounts.google.com."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for pg in context.pages:
            if not pg.is_closed() and GOOGLE_AUTH_URL_RE.match(pg.url):
                return pg
        await asyncio.sleep(0.3)
    return None


# Sélecteurs de l'écran Google (plusieurs variantes : le HTML de Google change selon la langue / le test A-B)
EMAIL_SELECTORS = (
    "input#identifierId",
    "input[name='identifier']",
    "input[type='email']",
    "input[autocomplete='username']",
    "input[aria-label*='mail' i]",
    "input[aria-label*='phone' i]",
)
PASSWORD_SELECTORS = (
    "input[name='Passwd']",
    "input[name='password']",
    "input[type='password']",
    "input[autocomplete='current-password']",
)
NEXT_RE = re.compile(r"^\s*(next|suivant)\s*$", re.I)


EMAIL_LABEL_RE = re.compile(r"email or phone|e-?mail ou t.l.phone", re.I)
PASSWORD_LABEL_RE = re.compile(r"^\s*(enter your )?password|^\s*mot de passe", re.I)


async def first_visible(page: Page, selectors, label_re: re.Pattern) -> object | None:
    """Premier champ visible parmi `selectors`, sinon repli par libellé `label_re` (propre à CE champ)."""
    for sel in selectors:
        try:
            for el in await page.locator(sel).all():
                if await el.is_visible():
                    return el
        except Exception:
            pass
    try:  # repli : par libellé
        el = page.get_by_label(label_re).first
        if await el.is_visible():
            return el
    except Exception:
        pass
    return None


async def type_into(field, value: str) -> None:
    """Saisit `value` et vérifie ; repli sur une frappe clavier si fill() n'a pas pris."""
    await field.click()
    await field.fill(value)
    if await field.input_value() != value:
        await field.press("Control+A")
        await field.press("Delete")
        await field.type(value, delay=40)
    if await field.input_value() != value:
        raise RuntimeError("la saisie n'a pas été prise en compte")


async def google_next(page: Page, field, step_id: str) -> None:
    """Clique sur « Next » (e-mail : #identifierNext, mot de passe : #passwordNext) ou appuie sur Entrée."""
    candidates = (
        page.locator(f"#{step_id} button"),
        page.locator(f"#{step_id}"),
        page.get_by_role("button", name=NEXT_RE).first,
    )
    for nxt in candidates:
        try:
            if await nxt.first.is_visible():
                await nxt.first.click(timeout=5000)
                return
        except Exception:
            pass
    await field.press("Enter")


async def google_oauth_skills(page: Page) -> None:
    """Parcourt les écrans Google : choix du compte, e-mail, mot de passe, consentement.

    Le mot de passe n'est soumis qu'UNE seule fois (jamais de nouvel essai : risque de verrouillage).
    Fin normale : popup refermée ou retour sur le site Skills. Blocage (2FA, captcha, mot de passe
    refusé…) : LabError avec capture de la page Google.
    """
    email_done = pwd_done = False
    last_error = ""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    deadline = time.monotonic() + GOOGLE_AUTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if page.is_closed():
            logger.info("Google : fenêtre refermée, retour sur Skills.")
            return  # popup refermée : Google a rendu la main à Skills
        url = page.url
        if url not in ("", "about:blank") and not GOOGLE_AUTH_URL_RE.match(url):
            logger.info("Google : redirigé vers %s", url.split("?")[0])
            return  # redirigé vers Skills
        try:
            text = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            text = ""  # navigation en cours
        problem = google_problem(text)
        if problem:
            raise LabError(problem, await snapshot(page))
        try:
            # a) « Choisir un compte »
            if not email_done:
                accounts = await page.locator("[data-identifier]:visible").all()
                clicked = False
                for el in accounts:
                    if ((await el.get_attribute("data-identifier")) or "").lower() == SKILLS_EMAIL.lower():
                        logger.info("Google : compte déjà proposé, sélection.")
                        await el.click()
                        clicked = True
                        break
                if clicked:
                    email_done = True
                    await asyncio.sleep(1)
                    continue
                if accounts:
                    other = page.get_by_text(re.compile(r"use another account|utiliser un autre compte", re.I)).first
                    if await is_vis(other):
                        await other.click()
                        await asyncio.sleep(1)
                        continue
            # b) saisie de l'e-mail puis « Next »
            if not email_done:
                em = await first_visible(page, EMAIL_SELECTORS, EMAIL_LABEL_RE)
                if em is not None:
                    logger.info("Google : champ e-mail trouvé, saisie.")
                    await type_into(em, SKILLS_EMAIL)
                    email_done = True
                    await google_next(page, em, "identifierNext")
                    logger.info("Google : e-mail envoyé (Next).")
                    await asyncio.sleep(2)
                    continue
            # c) saisie du mot de passe (une seule fois) puis « Next »
            if email_done and not pwd_done:
                pw = await first_visible(page, PASSWORD_SELECTORS, PASSWORD_LABEL_RE)
                if pw is not None:
                    logger.info("Google : champ mot de passe trouvé, saisie.")
                    await type_into(pw, SKILLS_PASSWORD)
                    pwd_done = True
                    await google_next(page, pw, "passwordNext")
                    logger.info("Google : mot de passe envoyé (Next).")
                    await asyncio.sleep(2)
                    continue
            # d) consentement / écrans d'information, seulement après la saisie des identifiants
            if pwd_done:
                cont = page.get_by_role("button", name=GOOGLE_CONTINUE_RE).first
                if await is_vis(cont):
                    await cont.click()
                    await asyncio.sleep(1)
                    continue
        except Exception as exc:  # navigation / fermeture de popup en cours : on reboucle, mais on garde la trace
            last_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}"
            logger.warning("Google : étape en erreur (%s)", redact(last_error, [SKILLS_PASSWORD]))
        await asyncio.sleep(0.7)
    etape = "mot de passe saisi" if pwd_done else ("e-mail saisi" if email_done else "aucune saisie")
    detail = f" Dernière erreur : {redact(last_error, [SKILLS_PASSWORD])}" if last_error else ""
    raise LabError(
        f"Connexion Google non terminée après {GOOGLE_AUTH_TIMEOUT_S} s (étape : {etape}).{detail}",
        await snapshot(page),
    )


async def _login_skills_steps(page: Page) -> None:
    await page.goto(SKILLS_BASE, wait_until="domcontentloaded")

    # 1) Trouver le bouton Google (accueil → « Sign in » de l'en-tête, une seule fois → bouton Google)
    btn = None
    header_clicked = False
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        btn = await find_google_button(page)
        if btn is not None:
            break
        if not header_clicked:
            try:
                for el in await sign_in_controls(page).all():
                    if await el.is_visible():
                        await el.click()
                        header_clicked = True
                        break
            except Exception:
                pass
        await page.wait_for_timeout(400)
    if btn is None:
        raise LabError("Bouton « Sign in with Google » introuvable sur Google Skills.")

    # 2) Clic : Google s'ouvre dans une popup ou dans le même onglet
    await btn.click()
    gpage = await wait_for_google_page(page.context)
    if gpage is None:
        raise LabError("La page de connexion Google ne s'est pas ouverte après le clic sur le bouton Google.")

    # 3) Authentification Google
    await google_oauth_skills(gpage)

    # 4) Retour sur Skills. Si l'onglet d'origine n'a pas rafraîchi son état (popup), on le recharge.
    if gpage is not page:
        await asyncio.sleep(2)
        if await is_signed_out(page):
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception:
                pass

    # 5) La connexion doit être CONFIRMÉE (sinon LabError) avant de continuer.
    await confirm_login(page)


async def login_skills(page: Page) -> None:
    """Se connecte à Google Skills via le bouton Google et VALIDE la connexion. Toute LabError embarque une capture."""
    try:
        await _login_skills_steps(page)
    except LabError as err:
        if err.screenshot is None:
            err.screenshot = await snapshot(page)
        raise


async def start_lab(page: Page) -> None:
    end_btn = page.get_by_role("button", name=re.compile(r"end lab", re.I)).first
    start_btn = page.get_by_role("button", name=re.compile(r"start lab", re.I)).first

    # Ouverture du lab en vérifiant la session : un visiteur non connecté voit « Sign in » et un
    # « Start Lab » désactivé. Dans ce cas on se reconnecte (une fois) puis on rouvre le lab.
    for attempt in (1, 2):
        await page.goto(LAB_URL, wait_until="domcontentloaded")

        # Attend le rendu : Start Lab, End Lab OU « Sign in » visible
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while True:
            if await is_vis(start_btn) or await is_vis(end_btn) or await is_signed_out(page):
                break
            if time.monotonic() >= deadline:
                raise LabError("La page du lab ne s'est pas affichée.", await snapshot(page))
            await page.wait_for_timeout(500)

        if not await is_signed_out(page):
            break  # connecté
        if attempt == 2:
            raise LabError(
                "Toujours déconnecté sur la page du lab, même après une nouvelle connexion "
                "(« Sign in » visible). Vérifie SKILLS_EMAIL / SKILLS_PASSWORD et LAB_URL.",
                await snapshot(page),
            )
        logger.warning("Page du lab affichée en visiteur : nouvelle connexion avant de réessayer.")
        try:
            await login_skills(page)
        except LabError as err:
            raise LabError(
                f"Session absente sur la page du lab et reconnexion impossible : {err}", err.screenshot
            ) from err

    if not await end_btn.is_visible():
        # « Start Lab » doit être cliquable (il reste désactivé tant qu'on n'est pas connecté)
        for _ in range(20):
            if await start_btn.is_enabled():
                break
            await page.wait_for_timeout(500)
        else:
            raise LabError(
                "Le bouton « Start Lab » reste désactivé alors que la session semble active.",
                await snapshot(page),
            )
        await start_btn.click()

        dialog = page.get_by_role("dialog").first
        try:
            await dialog.wait_for(state="visible", timeout=5000)
            if not AUTO_CONFIRM_DIALOG:
                raise LabError(
                    "Une boîte de dialogue demande de choisir le mode de paiement "
                    "(crédits / code). Lance le lab manuellement ou mets AUTO_CONFIRM_DIALOG=1."
                )
            await dialog.get_by_role(
                "button", name=re.compile(r"start|confirm|launch|use", re.I)
            ).first.click()
        except LabError:
            raise
        except Exception:
            pass  # pas de dialogue : le lab démarre directement

    await end_btn.wait_for(state="visible", timeout=LAB_START_TIMEOUT_MS)


async def read_connection_details(page: Page) -> dict:
    """Lit identifiants et ID projet dans le panneau Connection/Lab Details."""
    await page.get_by_text(re.compile(r"username", re.I)).first.wait_for(timeout=TIMEOUT_MS)
    text = await page.locator("body").inner_text()

    user_m = USER_RE.search(text)
    if not user_m:
        raise LabError("Identifiants du lab introuvables dans le panneau de connexion.")
    pwd_m = re.search(r"Password\s*[:\n]?\s*(\S+)", text[user_m.end():])
    proj_m = PROJECT_RE.search(text)

    return {
        "username": user_m.group(0),
        "password": pwd_m.group(1) if pwd_m else "",
        "project": proj_m.group(0) if proj_m else "",
    }


async def open_console(context: BrowserContext, lab_page: Page) -> Page:
    """Ouvre la console Cloud puis ferme la page du lab (jamais 2 onglets utiles).

    1) Si le bouton est un lien avec href : on ferme d'abord la page du lab,
       puis on ouvre la console dans un nouvel onglet unique.
    2) Sinon (ouverture par JavaScript) : on clique, puis on ferme aussitôt le lab.
    """
    link = lab_page.get_by_role("link", name=OPEN_CONSOLE_RE).first
    href = await link.get_attribute("href") if await link.count() else None

    if href and href.startswith("http"):
        await safe_close(lab_page)
        console = await context.new_page()
        await console.goto(href, wait_until="domcontentloaded")
        return console

    async with context.expect_page() as new_page_info:
        await lab_page.get_by_text(OPEN_CONSOLE_RE).first.click()
    console = await new_page_info.value
    await safe_close(lab_page)
    return console


async def google_login(page: Page, username: str, password: str) -> None:
    await page.wait_for_load_state("domcontentloaded")
    await page.locator("input[type='email']").fill(username)
    await page.get_by_role("button", name=re.compile(r"next", re.I)).first.click()

    pwd = page.locator("input[type='password']")
    await pwd.wait_for(state="visible", timeout=TIMEOUT_MS)
    await pwd.fill(password)
    await page.get_by_role("button", name=re.compile(r"next", re.I)).first.click()

    # Écrans Google optionnels (les conditions de la console sont gérées à part, avec le pays figé)
    for label in (r"i understand", r"^continue$"):
        await click_if_present(page, label, timeout=3000)


# ---------- Conditions d'utilisation de la console : pays figé ----------
_SHOWN_JS = (
    "el => el.tagName === 'SELECT' ? ((el.options[el.selectedIndex] || {}).text || '') "
    ": (el.value || el.innerText || el.textContent || '')"
)


async def select_country(page: Page, ctl) -> None:
    """Sélectionne explicitement CR_COUNTRY dans le menu déroulant « Country »."""
    tag = await ctl.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        await ctl.select_option(label=CR_COUNTRY)
    else:
        if tag == "input":
            await ctl.fill(CR_COUNTRY)
        else:
            await ctl.click()
        try:
            option = await locate(
                page,
                lambda f: f.get_by_role("option", name=re.compile(rf"^\s*{CR_COUNTRY}\s*$", re.I)).first,
                f"option {CR_COUNTRY}",
                timeout_ms=8000,
            )
            await option.click()
        except LabError:  # repli : saisie au clavier dans la liste
            await page.keyboard.type(CR_COUNTRY, delay=50)
            await page.keyboard.press("Enter")

    shown = await ctl.evaluate(_SHOWN_JS)
    if CR_COUNTRY.lower() not in (shown or "").lower():
        raise LabError(f"Impossible de sélectionner le pays « {CR_COUNTRY} » (affiché : « {shown} »).")


async def check_terms_boxes(ctl) -> None:
    """Coche toutes les cases visibles du bloc (boîte des conditions) qui contient le menu Country.

    Le conteneur est le plus proche ancêtre du menu qui contient une case à cocher : les cases
    du formulaire situé derrière la boîte ne sont jamais touchées.
    """
    container = ctl.locator("xpath=ancestor::*[.//input[@type='checkbox'] or .//*[@role='checkbox']][1]")
    if not await container.count():
        return
    boxes = container.get_by_role("checkbox")
    for i in range(await boxes.count()):
        box = boxes.nth(i)
        if not await box.is_visible():
            continue
        if not await box.is_checked():
            try:
                await box.check(timeout=3000)
            except Exception:
                await box.click(force=True)
        if not await box.is_checked():
            raise LabError("Impossible de cocher une case des conditions d'utilisation.")


async def accept_console_terms(page: Page) -> None:
    """Pays = Angola, cases cochées, puis clic sur « Agree and continue » (si la boîte s'affiche)."""
    try:
        ctl = await locate(
            page,
            lambda f: f.get_by_role("combobox", name=re.compile(r"country", re.I))
            .or_(f.get_by_label(re.compile(r"^\s*country", re.I)))
            .first,
            "Country",
            timeout_ms=TERMS_WAIT_S * 1000,
        )
    except LabError:
        logger.info("Pas de boîte de conditions d'utilisation : étape ignorée.")
        return

    await select_country(page, ctl)
    await check_terms_boxes(ctl)

    agree = await locate(
        page,
        lambda f: f.get_by_role("button", name=re.compile(r"agree and continue", re.I)).first,
        "bouton Agree and continue",
    )
    await agree.click()
    await agree.wait_for(state="hidden", timeout=TIMEOUT_MS)


# ---------- Formulaire Cloud Run : remplissage figé ----------
async def locate(page: Page, factory, what: str, timeout_ms: int | None = None, visible: bool = True):
    """Cherche un élément dans la page et ses iframes (polling)."""
    deadline = time.monotonic() + (timeout_ms or TIMEOUT_MS) / 1000
    while True:
        for frame in page.frames:
            try:
                loc = factory(frame)
                if await loc.count() and (not visible or await loc.is_visible()):
                    return loc
            except Exception:
                pass
        if time.monotonic() >= deadline:
            raise LabError(f"Élément introuvable : {what}.")
        await page.wait_for_timeout(500)


def text_input(label_pattern: str):
    """Fabrique un sélecteur qui ne cible qu'un <input>/<textarea> portant ce libellé."""
    return lambda f: f.get_by_label(re.compile(label_pattern, re.I)).and_(
        f.locator("input, textarea")
    ).first


async def set_text_field(page: Page, label_pattern: str, value: str, what: str) -> None:
    field = await locate(page, text_input(label_pattern), what)
    await field.fill(value)
    if await field.input_value() != value:  # repli : saisie clavier
        await field.click()
        await field.press("Control+A")
        await field.type(value, delay=15)
    if await field.input_value() != value:
        raise LabError(f"Impossible de renseigner « {what} » avec la valeur attendue.")


async def read_text_field(page: Page, label_pattern: str, what: str) -> str:
    field = await locate(page, text_input(label_pattern), what)
    return await field.input_value()


async def apply_text_fields(page: Page) -> None:
    await set_text_field(page, r"container image url", CR_IMAGE_URL, "Container image URL")
    # Le nom du service est pré-rempli à partir de l'image : on attend la fin de l'auto-remplissage
    await page.wait_for_timeout(2000)
    await set_text_field(page, r"^\s*service name", CR_SERVICE_NAME, "Service name")


async def text_fields_ok(page: Page) -> bool:
    image = await read_text_field(page, r"container image url", "Container image URL")
    name = await read_text_field(page, r"^\s*service name", "Service name")
    return image == CR_IMAGE_URL and name == CR_SERVICE_NAME


async def ensure_radio(
    page: Page, pattern: str, what: str, required: bool = True, timeout_ms: int | None = None
) -> None:
    """Sélectionne une option radio et vérifie qu'elle est bien cochée."""
    try:
        radio = await locate(
            page,
            lambda f: f.get_by_role("radio", name=re.compile(pattern, re.I)).first,
            what,
            timeout_ms=timeout_ms,
            visible=False,  # les radios Material ont souvent un input transparent
        )
    except LabError:
        if required:
            raise
        logger.warning("Option introuvable (laissée par défaut) : %s", what)
        return

    if not await radio.is_checked():
        try:
            await radio.check(timeout=5000)
        except Exception:
            await radio.click(force=True)
    if not await radio.is_checked():
        raise LabError(f"Impossible de sélectionner : {what}.")


async def region_error_visible(page: Page) -> bool:
    """True si la console affiche « Cloud Run regions can't be retrieved… » (API pas encore prête)."""
    for frame in page.frames:
        try:
            if await frame.get_by_text(REGION_ERROR_RE).first.is_visible():
                return True
        except Exception:
            pass
    return False


async def wait_regions_ok(page: Page) -> bool:
    """Clique sur « Retry » plusieurs fois (propagation de l'API). True si l'erreur a disparu."""
    await page.wait_for_timeout(2000)  # laisse le temps à la bannière d'apparaître
    for attempt in range(1, REGION_RETRIES + 1):
        if not await region_error_visible(page):
            return True
        logger.warning("Régions Cloud Run indisponibles, essai %d/%d", attempt, REGION_RETRIES)
        await page.wait_for_timeout(REGION_RETRY_WAIT_S * 1000)
        await click_if_present(page, r"^\s*retry\s*$", timeout=3000)
        await page.wait_for_timeout(4000)
    return not await region_error_visible(page)


async def enable_run_api(page: Page, project: str) -> None:
    """Dernier recours : active l'API Cloud Run Admin depuis la bibliothèque d'API."""
    if not project:
        raise LabError("API Cloud Run Admin non activée et ID du projet inconnu.")
    await page.goto(f"{API_LIBRARY_URL}?project={project}", wait_until="domcontentloaded")
    # Si l'API est déjà activée, le bouton affiché est « Manage » : aucun clic nécessaire
    await click_if_present(page, r"^\s*enable\s*$", timeout=20_000)
    await page.wait_for_timeout(API_PROPAGATION_S * 1000)


async def deploy_cloud_run(page: Page, project: str) -> None:
    """Remplit le formulaire « Create service » avec les valeurs figées, puis clique sur Create."""
    url = CONSOLE_RUN_URL.rstrip("/") + "/create"
    if project:
        url += f"?project={project}"

    for attempt in (1, 2):
        await page.goto(url, wait_until="domcontentloaded")

        # 0. Conditions d'utilisation : pays Angola, cases cochées, « Agree and continue »
        #    (acceptées une fois pour toutes : inutile de les rechercher à la 2e tentative)
        if attempt == 1:
            await accept_console_terms(page)

        # 1-2. Image puis nom du service (le nom est écrasé par l'auto-remplissage si fait avant)
        await apply_text_fields(page)

        # Régions : si l'API Cloud Run Admin n'est pas prête, « Retry » puis activation de l'API
        if await wait_regions_ok(page):
            break
        if attempt == 2:
            raise LabError(
                "Les régions Cloud Run restent indisponibles : l'API Cloud Run Admin "
                "n'est pas utilisable dans ce projet."
            )
        await enable_run_api(page, project)

    # 3. Options
    await ensure_radio(page, CR_AUTH_PATTERN, "Authentication : Allow public access")
    await ensure_radio(page, CR_BILLING_PATTERN, "Billing : Instance-based")
    await ensure_radio(page, CR_INGRESS_PATTERN, "Ingress : All", required=False, timeout_ms=8000)

    # Vérification finale juste avant de créer (protège contre un auto-remplissage tardif)
    if not await text_fields_ok(page):
        await apply_text_fields(page)
        if not await text_fields_ok(page):
            raise LabError("Les champs image / nom du service ne gardent pas les valeurs figées.")

    # Dernière vérification des régions avant de créer
    if await region_error_visible(page) and not await wait_regions_ok(page):
        raise LabError("Les régions Cloud Run sont devenues indisponibles avant « Create ».")

    # 4. Bouton Create
    create = await locate(
        page,
        lambda f: f.get_by_role("button", name=re.compile(r"^\s*create\s*$", re.I)).first,
        "bouton Create",
    )
    await create.click()
    await page.wait_for_url(re.compile(r"/run/detail/"), timeout=60_000)


async def get_run_domain(page: Page, project: str) -> list[str]:
    url = CONSOLE_RUN_URL
    if project:
        url += f"?project={project}"
    await page.goto(url, wait_until="domcontentloaded")

    urls = await find_run_urls(page, RUN_SEARCH_TIMEOUT_S // 2)
    if urls:
        return urls

    # Sinon, ouvre le premier service (l'URL figure sur la page de détail)
    try:
        await page.locator("a[href*='/run/detail/']").first.click(timeout=15000)
        await enforce_single_tab(page.context, page)  # sécurité : jamais d'onglet en plus
        return await find_run_urls(page, RUN_SEARCH_TIMEOUT_S)
    except Exception:
        return []


async def run_lab_flow(notify) -> dict:
    if not (SKILLS_EMAIL and SKILLS_PASSWORD and LAB_URL):
        raise LabError("Variables SKILLS_EMAIL, SKILLS_PASSWORD et LAB_URL manquantes.")

    secrets = [SKILLS_PASSWORD]  # à masquer dans tout message d'erreur

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        context = None
        try:
            state = load_storage_state()
            ctx_kwargs = {"storage_state": state} if state else {}
            context = await browser.new_context(
                viewport={"width": 1024, "height": 700},  # viewport réduit = moins de RAM
                locale="en-US",
                user_agent=USER_AGENT,
                **ctx_kwargs,  # storage_state="storage_state.json" si la session existe (voir load_storage_state)
            )
            await context.add_init_script(HIDE_WEBDRIVER_JS)  # aussi pour les popups Google (nouvelles pages)
            context.set_default_timeout(TIMEOUT_MS)
            if BLOCK_HEAVY_RESOURCES:
                await block_heavy_resources(context)

            page = await context.new_page()
            await page.add_init_script(HIDE_WEBDRIVER_JS)

            if state:
                await notify("🔐 Session enregistrée chargée : connexion Google ignorée.")
            else:
                await notify("🔐 Connexion à Google Skills (via Google)…")
                await login_skills(page)
                await notify("✅ Connexion validée.")

            await notify("🚀 Démarrage du lab (peut prendre 1 à 3 min)…")
            try:
                await start_lab(page)
            except LabError as err:
                if state and "Session absente" in str(err):
                    raise LabError(
                        f"{err} La session enregistrée a sans doute expiré : régénère storage_state.json.",
                        err.screenshot,
                    ) from err
                raise

            await notify("📋 Lecture des identifiants du lab…")
            details = await read_connection_details(page)
            secrets.append(details["password"])

            await notify("☁️ Ouverture de la console Google Cloud…")
            page = await open_console(context, page)  # la page du lab est fermée ici
            await enforce_single_tab(context, page)
            await google_login(page, details["username"], details["password"])

            await notify("🛠️ Déploiement du service Cloud Run…")
            await deploy_cloud_run(page, details["project"])

            await notify("⏳ Attente de l'URL du service (déploiement en cours)…")
            run_urls = await find_run_urls(
                page, DEPLOY_TIMEOUT_S, prefix=f"https://{CR_SERVICE_NAME}-"
            )
            if not run_urls:  # repli : cherche dans la liste des services
                run_urls = await get_run_domain(page, details["project"])

            await safe_close(page)  # extraction terminée : plus aucun onglet ouvert
            return {**details, "run_urls": run_urls}

        except Exception as exc:
            shot = None
            try:
                current = last_open_page(context) if context else None
                if current:
                    shot = await current.screenshot(type="png")
            except Exception:
                pass
            message = str(exc) if isinstance(exc, LabError) else f"{type(exc).__name__}: {exc}"
            raise LabError(redact(message, secrets), shot) from exc

        finally:
            # Nettoyage garanti, même en cas d'erreur
            if context is not None:
                await enforce_single_tab(context, None)
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass
            gc.collect()


# ---------- Handlers Telegram ----------
def is_authorized(update: Update) -> bool:
    return bool(ALLOWED_CHAT_ID) and update.effective_chat.id == ALLOWED_CHAT_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Bienvenue !\n\n"
        "/lab – démarrer le lab et récupérer les infos / le domaine *.run.app\n"
        "/start – afficher ce message\n\n"
        f"Ton chat ID : {update.effective_chat.id}"
    )


async def lab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text(
            "⛔ Non autorisé. Renseigne ALLOWED_CHAT_ID avec ton chat ID (visible via /start)."
        )
        return

    # Aucun await entre ce test et l'acquisition du verrou : pas de course possible
    if LAB_LOCK.locked():
        await update.message.reply_text("⏳ Un lab est déjà en cours de traitement.")
        return

    async with LAB_LOCK:
        msg = await update.message.reply_text("⏳ Démarrage…")

        async def notify(text: str) -> None:
            try:
                await msg.edit_text(text)
            except Exception:
                pass

        try:
            result = await run_lab_flow(notify)
        except LabError as err:
            logger.error("Échec du flux : %s", err)
            await notify(f"❌ Échec : {err}")
            if err.screenshot:
                try:
                    await update.message.reply_photo(err.screenshot, caption="Dernière capture")
                except Exception:
                    logger.exception("Envoi de la capture impossible")
            return
        except Exception:
            logger.exception("Erreur inattendue")
            await notify("❌ Erreur inattendue (voir les logs Render).")
            return

        urls = result["run_urls"]
        urls_txt = (
            "\n".join(html.escape(u) for u in urls)
            if urls
            else "Aucune URL trouvée (déploiement échoué ou trop long) : vérifie la console."
        )
        title = "✅ <b>Lab prêt</b>" if urls else "⚠️ <b>Lab prêt, mais URL Cloud Run introuvable</b>"
        text = (
            f"{title}\n\n"
            f"👤 <code>{html.escape(result['username'])}</code>\n"
            f"🔑 <code>{html.escape(result['password'])}</code>\n"
            f"📦 Projet : <code>{html.escape(result['project'] or 'n/a')}</code>\n"
            f"🧩 Service : <code>{CR_SERVICE_NAME}</code>\n\n"
            f"🌐 <b>Cloud Run</b>\n{urls_txt}"
        )
        try:
            await msg.edit_text(text[:4000], parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Envoi HTML impossible, repli en texte brut")
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]
            await update.message.reply_text(plain)


# ---------- FastAPI + cycle de vie du bot ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("lab", lab, block=False))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot Telegram démarré")

    yield

    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    logger.info("Bot Telegram arrêté")


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "ok"}
