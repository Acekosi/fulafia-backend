import os
import re
import time
import shutil
import zipfile
import logging
import functools
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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


def wait_for_loader(page: Page, timeout: int = 10000):
    """Wait for any Vue spinner/loader overlays to disappear."""
    try:
        page.wait_for_selector(
            ".v-overlay--active, .v-progress-circular, .loading, [class*='spinner']",
            state="hidden",
            timeout=timeout,
        )
    except Exception:
        pass


def click_sidebar_item(page: Page, item_text: str):
    """Click a sidebar menu item by its text (works with collapsed sidebar)."""
    logger.info(f"Clicking '{item_text}' in sidebar...")
    try:
        page.get_by_text(item_text, exact=True).first.click(force=True, timeout=10000)
    except Exception as e:
        logger.warning(f"First click on '{item_text}' failed ({e}), retrying once...")
        page.wait_for_timeout(2000)
        page.get_by_text(item_text, exact=True).first.click(force=True, timeout=15000)
    page.wait_for_timeout(1500)


def goto_fee_subpage(page: Page, subpage: str) -> bool:
    """Navigate straight to the Fees sub-page URL (same authenticated session),
    instead of clicking through the sidebar. Direct navigation sidesteps the
    case where a forced click updates the sidebar's own active-state without
    the router actually swapping in the target view (confirmed via debug
    screenshot: 'Standalone' shows highlighted in the sidebar while the main
    pane still renders the Dashboard)."""
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

    # This is a full page reload (fresh JS bundle + auth check + initial data
    # fetch), unlike the SPA's own client-side route swap — give it more room
    # to settle than a simple client-side transition would need.
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass  # best-effort — some portals keep a background poll alive forever

    wait_for_loader(page)
    page.wait_for_timeout(2500)

    if url_fragment in page.url:
        return True

    logger.warning(f"After direct navigation, URL is still '{page.url}'")
    return False


def click_fee_subpage(page: Page, subpage: str):
    """Click a sub-menu item under Fees after the dropdown has expanded.
    Kept as a fallback only — prefer goto_fee_subpage for normal use."""
    sub_text_map = {
        "school_only": "School Fee",
        "standalone_only": "Standalone",
    }
    # Fragment we expect to see in the URL once navigation actually lands
    # (see portal screenshots: .../dashboard/fees vs .../dashboard/stand-alone-fee)
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

        # Only (re-)open the "Fees" submenu if the target link isn't already
        # visible. "Fees" is a toggle — clicking it when already expanded
        # collapses it again and hides the very link we're about to click.
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
                continue  # try reopening the submenu on the next loop
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

        # Actively wait for the URL to change rather than sleeping a fixed
        # amount and checking once — some sub-pages (e.g. Standalone) route
        # noticeably slower than others (e.g. School Fee).
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
    """Select the target session. Tries a native <select> first (in case this
    page uses the same implementation as the Standalone semester dropdown),
    then falls back to the PrimeVue custom combobox/listbox overlay."""
    if not session or session == "all":
        return True

    logger.info(f"Applying session filter: {session}")

    # --- Strategy 1: native <select> element -------------------------------
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

    # --- Strategy 2: PrimeVue custom combobox/listbox overlay ---------------
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
            # The combobox may simply not have rendered yet — give the page
            # a bit more time before the retry rather than giving up.
            wait_for_loader(page)
            page.wait_for_timeout(2000)

    return False


def apply_semester_filter(page: Page, semester: str) -> bool:
    """Select the target semester. Handles two different UI implementations
    seen on the portal: a native <select> (confirmed on the Standalone page,
    e.g. <select><option value="28">First</option>...) and a PrimeVue custom
    combobox/listbox overlay (seen on the School Fee page)."""
    if not semester or semester == "all":
        return True

    logger.info(f"Applying semester filter: {semester}")

    # --- Strategy 1: native <select> element -------------------------------
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

    # --- Strategy 2: PrimeVue custom combobox/listbox overlay ---------------
    # NOTE: the live portal's option aria-label is just "First"/"Second"
    # (see dropdown screenshot) — NOT "First Semester". Match both, in
    # case the label format ever changes between pages.
    candidate_labels = [semester, f"{semester} Semester"]

    # Let any lingering overlay from the previous (session) combobox fully
    # settle before touching the next one — clicking too early sometimes
    # lands on a still-transitioning element and the listbox never opens.
    wait_for_loader(page)
    page.wait_for_timeout(1000)

    for attempt in range(2):  # one retry in case the first click is swallowed
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
                # The filter row (Search / All Categories / All Semesters)
                # can take a while to render on the Standalone page — poll
                # for it instead of giving up on a single check.
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
            # Give the options themselves a moment to populate (the listbox
            # container can appear before its contents finish rendering).
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
                # Try any element with role="option" anywhere (not just <li>),
                # and try both text and aria-label based matching.
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
                # Out of guesses — dump the listbox's actual markup so we can
                # see the real structure/labels in the log for next time.
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
    """On standalone page, check if category dropdown exists. Leave as default."""
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
    """Wait for download buttons/links to appear in the data table."""
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
    page: Page, download_dir: str, result: ExtractionResult, page_label: str
):
    """Click all download buttons on the current page and save files."""
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

    for index, button in enumerate(buttons):
        button_label = f"{page_label} #{index + 1}"
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


def navigate_and_download_school_fees(
    page: Page, download_dir: str, payload: ExtractionRequest, result: ExtractionResult
):
    """Handle the School Fees view: sidebar nav → session filter → semester filter → download.

    The School Fee page requires a semester to be explicitly selected before it
    shows any data — there is no unfiltered/"all semesters" view here. So when
    the request asks for "all" semesters, we go through First, then Second,
    applying the filter and downloading after each pass.
    """
    if not goto_fee_subpage(page, "school_only"):
        logger.warning("Direct navigation failed — falling back to sidebar clicks")
        click_sidebar_item(page, "Fees")
        click_fee_subpage(page, "school_only")
    wait_for_loader(page)
    page.wait_for_timeout(TABLE_SETTLE)

    session_ok = apply_session_filter(page, payload.session)
    if not session_ok:
        download_receipts_from_page(page, download_dir, result, "school_fee")
        return

    wait_for_loader(page)
    page.wait_for_timeout(FILTER_WAIT)

    if not payload.semester or payload.semester == "all":
        semesters_to_process = ["First", "Second"]
    else:
        semesters_to_process = [payload.semester]

    for sem in semesters_to_process:
        logger.info(f"School Fee: applying semester '{sem}'")
        apply_semester_filter(page, sem)
        wait_for_loader(page)
        page.wait_for_timeout(TABLE_SETTLE)

        # Distinct page_label per semester so filenames don't collide between
        # passes (each pass's button index otherwise restarts at #1).
        label = "school_fee" if len(semesters_to_process) == 1 else f"school_fee_{sem.lower()}"
        download_receipts_from_page(page, download_dir, result, label)


def navigate_and_download_standalone(
    page: Page, download_dir: str, payload: ExtractionRequest, result: ExtractionResult
):
    """Handle the Standalone view: sidebar nav → session filter → category/semester appear → download."""
    if not goto_fee_subpage(page, "standalone_only"):
        logger.warning("Direct navigation failed — falling back to sidebar clicks")
        click_sidebar_item(page, "Fees")
        click_fee_subpage(page, "standalone_only")
    wait_for_loader(page)
    page.wait_for_timeout(TABLE_SETTLE)

    session_ok = apply_session_filter(page, payload.session)
    if session_ok:
        wait_for_loader(page)
        page.wait_for_timeout(FILTER_WAIT * 2)

        apply_category_filter(page)
        apply_semester_filter(page, payload.semester)
        wait_for_loader(page)
        page.wait_for_timeout(TABLE_SETTLE)

    download_receipts_from_page(page, download_dir, result, "standalone")


def run_extraction(
    payload: ExtractionRequest, download_dir: str, zip_path: str
) -> ExtractionResult:
    result = ExtractionResult()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                # Keep this a genuine headed browser (avoids the headless
                # fingerprint that seems to trigger slowdowns/detection on
                # the portal) but move the window off-screen so it isn't
                # visually intrusive during normal use.
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
        # Mask the most common headless-detection signal (navigator.webdriver)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        context.set_default_timeout(NAVIGATION_TIMEOUT)
        page = context.new_page()

        try:
            logger.info("Navigating to login page...")
            page.goto("https://my.fulafia.edu.ng/login", wait_until="domcontentloaded")
            page.wait_for_load_state("domcontentloaded")
            wait_for_loader(page)

            logger.info("Filling login credentials...")
            page.fill(
                'input[type="text"], input[placeholder*="Matric"], '
                'input[id*="matric"], input[name*="matric"]',
                payload.matric_no,
            )
            page.fill('input[type="password"]', payload.password)

            logger.info("Submitting login...")
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
            logger.info(f"Logged in successfully. Current URL: {page.url}")
            page.wait_for_timeout(2000)  # let post-login bootstrap settle

            if payload.fee_type == "both":
                navigate_and_download_school_fees(page, download_dir, payload, result)
                logger.info("Switching to standalone fees view...")
                page.wait_for_timeout(FILTER_WAIT)
                navigate_and_download_standalone(page, download_dir, payload, result)
            elif payload.fee_type == "school_only":
                navigate_and_download_school_fees(page, download_dir, payload, result)
            elif payload.fee_type == "standalone_only":
                navigate_and_download_standalone(page, download_dir, payload, result)

            logger.info(f"Extraction complete: {result.summary}")

        finally:
            browser.close()

    return result


@app.post("/api/extract")
async def extract_receipts(
    payload: ExtractionRequest, background_tasks: BackgroundTasks
):
    download_dir, zip_path = get_safe_paths(payload.matric_no)

    cleanup_temp_files(download_dir, zip_path)
    os.makedirs(download_dir, exist_ok=True)

    try:
        loop = __import__("asyncio").get_event_loop()
        result: ExtractionResult = await loop.run_in_executor(
            executor,
            functools.partial(run_extraction, payload, download_dir, zip_path),
        )
    except RuntimeError as e:
        cleanup_temp_files(download_dir, zip_path)
        if "Invalid credentials" in str(e):
            raise HTTPException(status_code=401, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        cleanup_temp_files(download_dir, zip_path)
        raise
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        cleanup_temp_files(download_dir, zip_path)
        raise HTTPException(
            status_code=400, detail=f"Portal extraction failed: {str(e)}"
        )

    files_to_zip = [f for f in os.listdir(download_dir) if os.path.isfile(os.path.join(download_dir, f))]

    if not files_to_zip:
        cleanup_temp_files(download_dir, zip_path)
        if result.total_found == 0:
            raise HTTPException(status_code=404, detail=result.summary)
        raise HTTPException(status_code=400, detail=result.summary)

    with zipfile.ZipFile(zip_path, "w") as zipf:
        for file in files_to_zip:
            zipf.write(os.path.join(download_dir, file), file)

    background_tasks.add_task(cleanup_temp_files, download_dir, zip_path)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=os.path.basename(zip_path),
    )
