import os
import re
import time
import uuid
import shutil
import zipfile
import logging
import threading
import functools
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator
from playwright.sync_api import sync_playwright, Page, Locator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="E-tech FULafia Downloader Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://e-reciept.netlify.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
executor = ThreadPoolExecutor(max_workers=2)

NAVIGATION_TIMEOUT = 60000
DOWNLOAD_TIMEOUT = 60000
FILTER_WAIT = 3000
TABLE_SETTLE = 4000


# ---------------------------------------------------------------------------
# Job status tracking
# ---------------------------------------------------------------------------
_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _job_update(job_id: str, **kwargs):
    """Thread-safe helper to update a job's status dict."""
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _set_status(job_id: str, text: str):
    _job_update(job_id, status_text=text)
    logger.info(f"[Job {job_id[:8]}] {text}")


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _cleanup_job_files(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return
    download_dir = job.get("download_dir", "")
    zip_path = job.get("zip_path", "")
    cleanup_temp_files(download_dir, zip_path)


# ---------------------------------------------------------------------------
# Existing data classes & models (unchanged)
# ---------------------------------------------------------------------------
@dataclass
class ExtractionResult:
    downloaded: int = 0
    failed: int = 0
    total_found: int = 0
    failed_details: list = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return self.downloaded > 0 and self.failed == 0

    @property
    def summary(self) -> str:
        if self.downloaded == 0 and self.failed == 0:
            return "No invoices were found on the portal for the selected filters."
        parts = [f"Downloaded {self.downloaded} of {self.total_found} invoices"]
        if self.failed_details:
            parts.append(f"({self.failed} failed: {'; '.join(self.failed_details[:3])})")
        return " ".join(parts)


class ExtractionRequest(BaseModel):
    matric_no: str
    password: str
    fee_type: str
    session: str
    semester: str

    @field_validator("matric_no")
    @classmethod
    def validate_matric_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Matric number cannot be empty")
        if not re.match(r"^[0-9]{4}/[A-Z]+/[A-Z]+/[0-9]+$", v.upper()):
            raise ValueError(
                "Matric number must be in format: YEAR/FACULTY/DEPT/NUMBER (e.g. 2023/CP/CYB/0000)"
            )
        return v.upper()


# ---------------------------------------------------------------------------
# Utility helpers (unchanged)
# ---------------------------------------------------------------------------
def cleanup_temp_files(folder_path: str, zip_path: str):
    try:
        if os.path.exists(folder_path):
            shutil.rmtree(folder_path)
    except Exception as e:
        logger.warning(f"Failed to cleanup folder {folder_path}: {e}")
    try:
        if os.path.exists(zip_path):
            os.remove(zip_path)
    except Exception as e:
        logger.warning(f"Failed to cleanup zip {zip_path}: {e}")


def get_safe_paths(matric_no: str):
    safe_matric = matric_no.replace("/", "_")
    download_dir = os.path.join(BASE_DIR, f"temp_{safe_matric}")
    zip_path = os.path.join(BASE_DIR, f"FULafia_Receipts_{safe_matric}.zip")
    return download_dir, zip_path


# ---------------------------------------------------------------------------
# Playwright helpers (unchanged)
# ---------------------------------------------------------------------------
def wait_for_loader(page: Page, timeout: int = 10000):
    try:
        page.wait_for_selector(
            ".v-overlay--active, .v-progress-circular, .loading, [class*='spinner']",
            state="hidden",
            timeout=timeout,
        )
    except Exception:
        pass


def click_sidebar_item(page: Page, item_text: str):
    logger.info(f"Clicking '{item_text}' in sidebar...")
    try:
        page.get_by_text(item_text, exact=True).first.click(force=True, timeout=10000)
    except Exception as e:
        logger.warning(f"First click on '{item_text}' failed ({e}), retrying once...")
        page.wait_for_timeout(2000)
        page.get_by_text(item_text, exact=True).first.click(force=True, timeout=15000)
    page.wait_for_timeout(1500)


def goto_fee_subpage(page: Page, subpage: str) -> bool:
    url_map = {
        "school_only": "https://my.fulafia.edu.ng/dashboard/fees",
        "standalone_only": "https://my.fulafia.edu.ng/dashboard/stand-alone-fee",
    }
    url_fragment_map = {
        "school_only": "fees",
        "standalone_only": "stand-alone-fee",
    }
    target_url = url_map[subpage]
    url_fragment = url_fragment_map[subpage]

    logger.info(f"Navigating directly to {target_url} ...")
    for attempt in range(2):
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            break
        except Exception as e:
            logger.warning(
                f"goto() attempt {attempt + 1} to {target_url} raised: {e} "
                "— checking current URL anyway, since the page may have "
                "partially loaded despite the timeout."
            )
            if url_fragment in page.url:
                logger.info(
                    "URL already reflects the target page despite the "
                    "timeout — continuing."
                )
                break
            if attempt == 1:
                return False
            page.wait_for_timeout(2000)

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    wait_for_loader(page)
    page.wait_for_timeout(2500)

    if url_fragment in page.url:
        return True

    logger.warning(f"After direct navigation, URL is still '{page.url}'")
    return False


def click_fee_subpage(page: Page, subpage: str):
    sub_text_map = {
        "school_only": "School Fee",
        "standalone_only": "Standalone",
    }
    url_fragment_map = {
        "school_only": "fees",
        "standalone_only": "stand-alone-fee",
    }
    target_text = sub_text_map[subpage]
    url_fragment = url_fragment_map[subpage]
    logger.info(f"Clicking '{target_text}' sub-menu...")

    for attempt in range(2):
        page.wait_for_timeout(2000)

        sub = page.get_by_text(target_text, exact=True).first

        try:
            is_visible = sub.is_visible()
        except Exception:
            is_visible = False

        if attempt > 0 and not is_visible:
            logger.info("'Fees' submenu appears collapsed — reopening it...")
            try:
                click_sidebar_item(page, "Fees")
                page.wait_for_timeout(1500)
            except Exception as e:
                logger.warning(f"Could not reopen 'Fees' submenu: {e}")

        try:
            sub.wait_for(state="visible", timeout=10000)
        except Exception:
            logger.warning(
                f"'{target_text}' not visible after wait (attempt {attempt + 1})"
            )
            if attempt == 0:
                continue
            else:
                logger.warning(
                    f"Giving up on '{target_text}' sub-menu — it never became visible."
                )
                return

        try:
            sub.click(force=True, timeout=10000)
        except Exception as e:
            logger.warning(f"Click on '{target_text}' failed: {e}")
            continue

        try:
            page.wait_for_url(lambda url: url_fragment in url, timeout=15000)
        except Exception:
            pass

        wait_for_loader(page)

        if url_fragment in page.url:
            return

        logger.warning(
            f"Attempt {attempt + 1}: URL is '{page.url}', expected fragment "
            f"'{url_fragment}' not found — navigation may not have landed. Retrying..."
        )

    logger.warning(
        f"Could not confirm navigation to '{target_text}' page after retries "
        f"(current URL: {page.url}). Continuing anyway."
    )


def apply_session_filter(page: Page, session: str) -> bool:
    if not session or session == "all":
        return True

    logger.info(f"Applying session filter: {session}")

    try:
        selects = page.locator("select").all()
        for sel in selects:
            try:
                option_texts = [t.strip() for t in sel.locator("option").all_inner_texts()]
            except Exception:
                continue
            if any(t == session.strip() for t in option_texts):
                sel.select_option(label=session)
                logger.info(f"Session '{session}' selected via native <select>")
                wait_for_loader(page)
                page.wait_for_timeout(3000)
                return True
    except Exception as e:
        logger.info(f"No matching native <select> for session (will try PrimeVue-style): {e}")

    for attempt in range(2):
        try:
            combobox = page.locator('span.p-select-label[role="combobox"]').first
            combobox.wait_for(state="visible", timeout=25000)
            combobox.click()
            logger.info("Session combobox clicked, waiting for listbox overlay...")

            page.wait_for_selector('ul[role="listbox"]', state="visible", timeout=8000)

            option = page.locator(
                f'li[role="option"][aria-label="{session}"]'
            ).first
            option.wait_for(state="visible", timeout=5000)
            option.click()
            logger.info(f"Session '{session}' selected")
            page.wait_for_timeout(3000)
            return True

        except Exception as e:
            logger.warning(f"Session filter attempt {attempt + 1} failed: {e}")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass
            wait_for_loader(page)
            page.wait_for_timeout(2000)

    return False


def apply_semester_filter(page: Page, semester: str) -> bool:
    if not semester or semester == "all":
        return True

    logger.info(f"Applying semester filter: {semester}")

    try:
        selects = page.locator("select").all()
        for sel in selects:
            try:
                option_texts = [t.strip() for t in sel.locator("option").all_inner_texts()]
            except Exception:
                continue
            if any(t.lower() == semester.strip().lower() for t in option_texts):
                sel.select_option(label=semester)
                logger.info(f"Semester '{semester}' selected via native <select>")
                wait_for_loader(page)
                page.wait_for_timeout(3000)
                return True
    except Exception as e:
        logger.info(f"No matching native <select> for semester (will try PrimeVue-style): {e}")

    candidate_labels = [semester, f"{semester} Semester"]

    wait_for_loader(page)
    page.wait_for_timeout(1000)

    for attempt in range(2):
        try:
            comboboxes = page.locator('span.p-select-label[role="combobox"]').all()
            semester_box = None
            for box in comboboxes:
                try:
                    label_text = box.inner_text().strip()
                    if "semester" in label_text.lower():
                        semester_box = box
                        break
                except Exception:
                    continue

            if not semester_box:
                poll_deadline = time.time() + 15
                while not semester_box and time.time() < poll_deadline:
                    page.wait_for_timeout(1500)
                    comboboxes = page.locator(
                        'span.p-select-label[role="combobox"]'
                    ).all()
                    for box in comboboxes:
                        try:
                            label_text = box.inner_text().strip()
                            if "semester" in label_text.lower():
                                semester_box = box
                                break
                        except Exception:
                            continue

            if not semester_box:
                if len(comboboxes) >= 2:
                    semester_box = comboboxes[-1]
                else:
                    logger.warning(
                        "Could not locate semester combobox after polling"
                    )
                    return False

            semester_box.click()
            logger.info(
                f"Semester combobox clicked (attempt {attempt + 1}), "
                "waiting for listbox overlay..."
            )

            page.wait_for_selector('ul[role="listbox"]', state="visible", timeout=8000)
            page.wait_for_timeout(500)

            option = None
            for label in candidate_labels:
                loc = page.locator(f'li[role="option"][aria-label="{label}"]').first
                try:
                    loc.wait_for(state="visible", timeout=1500)
                    option = loc
                    break
                except Exception:
                    continue

            if option is None:
                for selector in (
                    f'[role="option"]:has-text("{semester}")',
                    f'li:has-text("{semester}")',
                ):
                    loc = page.locator(selector).first
                    try:
                        loc.wait_for(state="visible", timeout=1500)
                        option = loc
                        break
                    except Exception:
                        continue

            if option is None:
                try:
                    listbox = page.locator('ul[role="listbox"]').first
                    html = listbox.inner_html()
                    logger.warning(
                        f"Semester listbox HTML (first 1500 chars): {html[:1500]}"
                    )
                except Exception as dump_err:
                    logger.warning(f"Could not dump listbox HTML: {dump_err}")
                raise RuntimeError("No matching semester option found")

            option.click()
            logger.info(f"Semester '{semester}' selected")
            wait_for_loader(page)
            page.wait_for_timeout(3000)
            return True

        except Exception as e:
            logger.warning(f"Semester filter attempt {attempt + 1} failed: {e}")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass

    logger.warning("Semester filter failed after retries (continuing anyway)")
    return False


def apply_category_filter(page: Page):
    try:
        comboboxes = page.locator('span.p-select-label[role="combobox"]').all()
        for box in comboboxes:
            try:
                label_text = box.inner_text().strip()
                if "Categor" in label_text or "categor" in label_text.lower():
                    logger.info(f"Category filter found with value '{label_text}', leaving as-is")
                    return
            except Exception:
                continue
    except Exception:
        pass


def wait_for_download_buttons(page: Page, timeout: int = 25000) -> list:
    logger.info("Waiting for download buttons to appear...")
    try:
        page.wait_for_selector(
            'button:has-text("Download Invoice"), '
            'button:has-text("Download"), '
            'a:has-text("Download Invoice"), '
            'a:has-text("Download")',
            state="visible",
            timeout=timeout,
        )
    except Exception:
        logger.warning("Timeout waiting for download buttons to become visible")

    wait_for_loader(page)

    buttons = page.locator(
        'button:has-text("Download Invoice"), '
        'button:has-text("Download"), '
        'button:has-text("download"), '
        'a:has-text("Download Invoice"), '
        'a:has-text("Download")'
    ).all()

    filtered = []
    for btn in buttons:
        try:
            if btn.is_visible():
                text = btn.inner_text().strip().lower()
                if "download" in text:
                    filtered.append(btn)
        except Exception:
            continue

    return filtered


def download_receipts_from_page(
    page: Page, download_dir: str, result: ExtractionResult, page_label: str,
    job_id: str | None = None,
):
    buttons = wait_for_download_buttons(page)

    if not buttons:
        logger.warning(f"No download buttons found on {page_label} page")
        debug_path = os.path.join(BASE_DIR, f"debug_{page_label}_last_failure.png")
        try:
            page.screenshot(path=debug_path, full_page=True)
            logger.warning(f"Saved debug screenshot to {debug_path}")
        except Exception:
            pass
        return

    result.total_found += len(buttons)
    logger.info(f"Found {len(buttons)} download buttons on {page_label} page")

    if job_id:
        _set_status(job_id, f"Found {len(buttons)} receipt(s) on {page_label.replace('_', ' ')} page. Downloading...")

    for index, button in enumerate(buttons):
        button_label = f"{page_label} #{index + 1}"
        if job_id:
            _set_status(job_id, f"Downloading receipt {index + 1} of {len(buttons)}...")
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as download_info:
                button.click()

            download = download_info.value
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', download.suggested_filename or f"Receipt_{index + 1}.pdf")
            save_path = os.path.join(download_dir, f"{page_label}_{index + 1}_{safe_name}")
            download.save_as(save_path)
            result.downloaded += 1
            logger.info(f"Downloaded {button_label}: {os.path.basename(save_path)}")
        except Exception as e:
            result.failed += 1
            msg = f"{button_label}: {str(e)[:80]}"
            result.failed_details.append(msg)
            logger.warning(f"Failed {button_label}: {e}")

    if job_id:
        _set_status(job_id, f"Finished downloading {page_label.replace('_', ' ')} receipts ({result.downloaded} ok, {result.failed} failed).")


def navigate_and_download_school_fees(
    page: Page, download_dir: str, payload: ExtractionRequest, result: ExtractionResult,
    job_id: str | None = None,
):
    if job_id:
        _set_status(job_id, "Navigating to School Fees page...")
    if not goto_fee_subpage(page, "school_only"):
        logger.warning("Direct navigation failed — falling back to sidebar clicks")
        click_sidebar_item(page, "Fees")
        click_fee_subpage(page, "school_only")
    wait_for_loader(page)
    page.wait_for_timeout(TABLE_SETTLE)

    if job_id:
        _set_status(job_id, "Applying session filter...")
    session_ok = apply_session_filter(page, payload.session)
    if not session_ok:
        download_receipts_from_page(page, download_dir, result, "school_fee", job_id)
        return

    wait_for_loader(page)
    page.wait_for_timeout(FILTER_WAIT)

    if not payload.semester or payload.semester == "all":
        semesters_to_process = ["First", "Second"]
    else:
        semesters_to_process = [payload.semester]

    for sem in semesters_to_process:
        logger.info(f"School Fee: applying semester '{sem}'")
        if job_id:
            _set_status(job_id, f"Applying {sem} semester filter (School Fees)...")
        apply_semester_filter(page, sem)
        wait_for_loader(page)
        page.wait_for_timeout(TABLE_SETTLE)

        label = "school_fee" if len(semesters_to_process) == 1 else f"school_fee_{sem.lower()}"
        download_receipts_from_page(page, download_dir, result, label, job_id)


def navigate_and_download_standalone(
    page: Page, download_dir: str, payload: ExtractionRequest, result: ExtractionResult,
    job_id: str | None = None,
):
    if job_id:
        _set_status(job_id, "Navigating to Standalone Fees page...")
    if not goto_fee_subpage(page, "standalone_only"):
        logger.warning("Direct navigation failed — falling back to sidebar clicks")
        click_sidebar_item(page, "Fees")
        click_fee_subpage(page, "standalone_only")
    wait_for_loader(page)
    page.wait_for_timeout(TABLE_SETTLE)

    if job_id:
        _set_status(job_id, "Applying session filter...")
    session_ok = apply_session_filter(page, payload.session)
    if session_ok:
        wait_for_loader(page)
        page.wait_for_timeout(FILTER_WAIT * 2)

        apply_category_filter(page)
        if job_id:
            _set_status(job_id, "Applying semester filter (Standalone)...")
        apply_semester_filter(page, payload.semester)
        wait_for_loader(page)
        page.wait_for_timeout(TABLE_SETTLE)

    download_receipts_from_page(page, download_dir, result, "standalone", job_id)


# ---------------------------------------------------------------------------
# Main extraction (now accepts job_id for live status updates)
# ---------------------------------------------------------------------------
def run_extraction(
    payload: ExtractionRequest, download_dir: str, zip_path: str,
    job_id: str,
) -> ExtractionResult:
    result = ExtractionResult()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-position=-2400,-2400",
                "--window-size=1366,800",
            ],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1366, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        context.set_default_timeout(NAVIGATION_TIMEOUT)
        page = context.new_page()

        try:
            _set_status(job_id, "Launching browser and navigating to portal...")
            page.goto("https://my.fulafia.edu.ng/login", wait_until="domcontentloaded")
            page.wait_for_load_state("domcontentloaded")
            wait_for_loader(page)

            _set_status(job_id, "Entering login credentials...")
            page.fill(
                'input[type="text"], input[placeholder*="Matric"], '
                'input[id*="matric"], input[name*="matric"]',
                payload.matric_no,
            )
            page.fill('input[type="password"]', payload.password)

            _set_status(job_id, "Submitting login form...")
            page.click(
                'button[type="submit"], button:has-text("Login"), '
                'button:has-text("Sign In")'
            )

            try:
                page.wait_for_url("**/dashboard**", timeout=30000)
            except Exception:
                if "login" in page.url.lower():
                    raise RuntimeError(
                        "Invalid credentials. Please check your matric number and password."
                    )
                raise

            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(FILTER_WAIT)
            wait_for_loader(page)
            _set_status(job_id, "Login successful! Loading dashboard...")
            page.wait_for_timeout(2000)

            if payload.fee_type == "both":
                navigate_and_download_school_fees(page, download_dir, payload, result, job_id)
                _set_status(job_id, "Switching to Standalone Fees view...")
                page.wait_for_timeout(FILTER_WAIT)
                navigate_and_download_standalone(page, download_dir, payload, result, job_id)
            elif payload.fee_type == "school_only":
                navigate_and_download_school_fees(page, download_dir, payload, result, job_id)
            elif payload.fee_type == "standalone_only":
                navigate_and_download_standalone(page, download_dir, payload, result, job_id)

            logger.info(f"Extraction complete: {result.summary}")

        finally:
            browser.close()

    return result


# ---------------------------------------------------------------------------
# Background worker that runs the extraction and stores results
# ---------------------------------------------------------------------------
def _background_extract(job_id: str, payload: ExtractionRequest):
    download_dir, zip_path = get_safe_paths(payload.matric_no)
    os.makedirs(download_dir, exist_ok=True)

    with _jobs_lock:
        _jobs[job_id]["download_dir"] = download_dir
        _jobs[job_id]["zip_path"] = zip_path

    try:
        result: ExtractionResult = run_extraction(payload, download_dir, zip_path, job_id)

        files_to_zip = [
            f for f in os.listdir(download_dir)
            if os.path.isfile(os.path.join(download_dir, f))
        ]

        if not files_to_zip:
            if result.total_found == 0:
                _job_update(
                    job_id,
                    state="error",
                    status_text=result.summary,
                    detail=result.summary,
                )
            else:
                _job_update(
                    job_id,
                    state="error",
                    status_text=result.summary,
                    detail=result.summary,
                )
            cleanup_temp_files(download_dir, zip_path)
            return

        _set_status(job_id, "Packaging receipts into ZIP file...")

        with zipfile.ZipFile(zip_path, "w") as zipf:
            for file in files_to_zip:
                zipf.write(os.path.join(download_dir, file), file)

        _job_update(job_id, state="complete", status_text="Extraction complete!")
        logger.info(f"Job {job_id[:8]} complete — ZIP ready at {zip_path}")

    except RuntimeError as e:
        if "Invalid credentials" in str(e):
            _job_update(job_id, state="error", status_text="Login failed: invalid credentials.", detail=str(e))
        else:
            _job_update(job_id, state="error", status_text=f"Extraction failed: {e}", detail=str(e))
        cleanup_temp_files(download_dir, zip_path)
    except Exception as e:
        logger.error(f"Job {job_id[:8]} failed: {e}")
        _job_update(job_id, state="error", status_text=f"Extraction failed: {e}", detail=str(e))
        cleanup_temp_files(download_dir, zip_path)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.post("/api/extract")
async def start_extraction(payload: ExtractionRequest):
    job_id = uuid.uuid4().hex
    download_dir, zip_path = get_safe_paths(payload.matric_no)

    cleanup_temp_files(download_dir, zip_path)

    with _jobs_lock:
        _jobs[job_id] = {
            "state": "processing",
            "status_text": "Queued...",
            "detail": None,
            "download_dir": None,
            "zip_path": None,
            "payload_matric": payload.matric_no,
            "payload_session": payload.session,
        }

    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(executor, functools.partial(_background_extract, job_id, payload))

    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse({
        "state": job["state"],
        "status_text": job["status_text"],
        "detail": job.get("detail"),
    })


@app.get("/api/download/{job_id}")
async def download_result(job_id: str, background_tasks: BackgroundTasks):
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["state"] != "complete":
        raise HTTPException(status_code=400, detail="Job is not complete yet.")

    zip_path = job["zip_path"]
    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="ZIP file no longer available.")

    matric = job.get("payload_matric", "unknown")
    session = job.get("payload_session", "unknown")
    safe_session = session.replace("/", "-")
    filename = f"FULafia_Receipts_{matric.replace('/', '_')}_{safe_session}.zip"

    background_tasks.add_task(_cleanup_job_files, job_id)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
    )
