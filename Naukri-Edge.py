"""
Naukri Auto-Apply Bot - Edge Version WITH CONTROL PANEL
================================================================================
Same automation as the original bot, plus:

  1. A Tkinter "Control Panel" window with Pause / Resume / Stop / Cancel Job
     buttons - no code editing needed to steer the run.
  2. Default pause checkpoints:
       - Before collecting jobs (confirms keyword/page count)
       - After collecting jobs (review list, tick/untick which to apply to)
  3. Mid-application control:
       - PAUSE  -> freezes the loop right where it is
       - STOP   -> hands the browser to you completely (do whatever you want
                   manually on the open tab); script just idles
       - CANCEL JOB -> abandons the job currently open, moves to next
       - RESUME -> re-scans the CURRENT browser window:
                     * if a submit button is visible -> clicks it
                     * if it finds question/input fields -> tries to answer
                       from ANSWER_BANK, or asks you via a popup if unknown
                     * otherwise assumes nothing left to do -> moves on

Usage:
    1. Copy .env.example to .env and fill in your details
       (optionally add ANSWER_BANK='{"notice period":"30 days"}')
    2. pip install -r requirements.txt
    3. python Naukri-Edge-Controlled.py
"""

import os
import re
import sys
import time
import json
import logging
import threading
import queue
import pandas as pd
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from bs4 import BeautifulSoup

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    WEBDRIVER_MANAGER_AVAILABLE = False

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
load_dotenv()

NAUKRI_EMAIL = os.getenv('NAUKRI_EMAIL', '')
NAUKRI_PASSWORD = os.getenv('NAUKRI_PASSWORD', '')

FIRSTNAME = os.getenv('FIRSTNAME', '')
LASTNAME = os.getenv('LASTNAME', '')

KEYWORDS = [kw.strip() for kw in os.getenv('KEYWORDS', '').split(',') if kw.strip()]
LOCATION = os.getenv('LOCATION', '').strip()

MAX_APPLICATIONS = int(os.getenv('MAX_APPLICATIONS', '50'))
PAGES_PER_KEYWORD = int(os.getenv('PAGES_PER_KEYWORD', '2'))

EDGE_DRIVER_PATH = os.getenv('EDGE_DRIVER_PATH', '')

# ------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Known answers for screening questions the bot may hit on resume.
# Preferred: put them in a separate readable file (see ANSWER_BANK_FILE below),
# since .env does not support multi-line values.
# Fallback: a compact single-line JSON blob directly in .env as ANSWER_BANK.
ANSWER_BANK_FILE = os.getenv('ANSWER_BANK_FILE', 'answer_bank.json')

ANSWER_BANK = {}
if os.path.exists(ANSWER_BANK_FILE):
    try:
        with open(ANSWER_BANK_FILE, 'r', encoding='utf-8') as f:
            ANSWER_BANK = json.load(f)
        logger.info(f"Loaded {len(ANSWER_BANK)} answers from {ANSWER_BANK_FILE}")
    except json.JSONDecodeError as e:
        logger.error(f"{ANSWER_BANK_FILE} has invalid JSON: {e}")
elif os.getenv('ANSWER_BANK'):
    try:
        ANSWER_BANK = json.loads(os.getenv('ANSWER_BANK'))
    except json.JSONDecodeError:
        logger.error("ANSWER_BANK in .env has invalid JSON, ignoring it.")


def validate_config():
    errors = []
    if not NAUKRI_EMAIL:
        errors.append("NAUKRI_EMAIL is not set in .env")
    if not NAUKRI_PASSWORD:
        errors.append("NAUKRI_PASSWORD is not set in .env")
    if not KEYWORDS:
        errors.append("KEYWORDS is not set in .env (comma-separated job roles)")
    if not FIRSTNAME:
        errors.append("FIRSTNAME is not set in .env")
    if not LASTNAME:
        errors.append("LASTNAME is not set in .env")
    if errors:
        for e in errors:
            logger.error(e)
        logger.error("Please copy .env.example to .env and fill in your details.")
        return False
    return True


# ================================================================
# CONTROL STATE - shared between the worker (automation) thread
# and the Tkinter GUI thread. Keep this dumb and explicit.
# ================================================================
class ShutdownRequested(Exception):
    """Raised inside the worker thread when the user quits the app."""
    pass


class ControlState:
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    MANUAL = "MANUAL"

    def __init__(self):
        self.state = self.RUNNING
        self._lock = threading.Lock()
        self.resume_event = threading.Event()
        self.resume_event.set()

        # one-shot flags, consumed by the worker loop
        self.rescan_on_resume = False
        self.request_cancel_job = False
        self.shutdown = False

        # helpMe mode: button-press signal queue ("SCAN" / "QUIT")
        self.scan_queue = queue.Queue()

    def set_state(self, new_state):
        with self._lock:
            self.state = new_state
        if new_state == self.RUNNING:
            self.resume_event.set()
        else:
            self.resume_event.clear()

    def get_state(self):
        with self._lock:
            return self.state


def check_shutdown(control: ControlState):
    if control.shutdown:
        raise ShutdownRequested()


# ================================================================
# CONTROL PANEL (Tkinter GUI)
# ================================================================
class ControlPanel:
    def __init__(self, control: ControlState):
        self.control = control
        self.root = tk.Tk()
        self.root.title("Naukri Auto-Apply - Control Panel")
        self.root.geometry("560x440")
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(self.root, textvariable=self.status_var,
                  font=("Segoe UI", 11, "bold"), wraplength=520).pack(pady=8)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(pady=4)

        self.pause_btn = ttk.Button(btn_frame, text="Pause", command=self.on_pause)
        self.pause_btn.grid(row=0, column=0, padx=4)

        self.resume_btn = ttk.Button(btn_frame, text="Resume (scan & continue)",
                                      command=self.on_resume, state="disabled")
        self.resume_btn.grid(row=0, column=1, padx=4)

        self.stop_btn = ttk.Button(btn_frame, text="Stop (hand me control)",
                                    command=self.on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=2, padx=4)

        self.cancel_btn = ttk.Button(btn_frame, text="Cancel Job (skip)",
                                      command=self.on_cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=3, padx=4)

        self.scan_btn = ttk.Button(self.root, text="Scan This Page & Apply All",
                                    command=self.on_scan, state="disabled")
        self.scan_btn.pack(pady=4)

        ttk.Button(self.root, text="QUIT SCRIPT", command=self.on_quit).pack(pady=6)

        ttk.Label(self.root, text="Log:").pack(anchor="w", padx=10)
        self.log_box = tk.Text(self.root, height=16, width=68, state="disabled")
        self.log_box.pack(padx=10, pady=6)

    # ---------------- logging / status (thread-safe) ----------------
    def log(self, msg):
        self.root.after(0, self._log, msg)

    def _log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def set_status(self, text):
        self.root.after(0, self.status_var.set, text)

    def finish(self):
        """Call once the worker thread has ended - nothing left to pause/resume."""
        def disable_all():
            self.pause_btn.config(state="disabled")
            self.resume_btn.config(state="disabled")
            self.stop_btn.config(state="disabled")
            self.cancel_btn.config(state="disabled")
            self.scan_btn.config(state="disabled")
        self.root.after(0, disable_all)

    def info(self, msg):
        self.root.after(0, lambda: messagebox.showinfo("Info", msg))

    # ---------------- button handlers ----------------
    def on_pause(self):
        self.control.set_state(ControlState.PAUSED)
        self.pause_btn.config(state="disabled")
        self.resume_btn.config(state="normal")
        self.stop_btn.config(state="normal")
        self.cancel_btn.config(state="normal")
        self.log(">> PAUSE requested.")

    def on_resume(self):
        self.control.rescan_on_resume = True
        self.control.set_state(ControlState.RUNNING)
        self._reset_buttons_running()
        self.log(">> RESUME - will scan current page before moving on.")

    def on_stop(self):
        self.control.set_state(ControlState.MANUAL)
        self.stop_btn.config(state="disabled")
        self.cancel_btn.config(state="disabled")
        self.log(">> STOP - you have full manual control of the browser. "
                  "Click 'Resume' when you want the bot to take back over.")

    def on_cancel(self):
        self.control.request_cancel_job = True
        self.control.set_state(ControlState.RUNNING)
        self._reset_buttons_running()
        self.log(">> CANCEL JOB - skipping current job, moving to next.")

    def on_scan(self):
        self.log(">> SCAN THIS PAGE requested.")
        self.control.scan_queue.put("SCAN")

    def enable_scan_button(self):
        self.root.after(0, lambda: self.scan_btn.config(state="normal"))

    def _reset_buttons_running(self):
        self.pause_btn.config(state="normal")
        self.resume_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.cancel_btn.config(state="disabled")

    def on_quit(self):
        if messagebox.askyesno("Quit", "Stop the bot completely?"):
            self.control.shutdown = True
            self.control.set_state(ControlState.RUNNING)  # unblock any wait loop
            self.control.scan_queue.put("QUIT")            # unblock helpMe scan-wait loop
            self.root.after(300, self.root.destroy)

    # ---------------- blocking dialogs (safe to call from worker thread) ----------------
    def confirm_checkpoint(self, message, options=("Continue", "Cancel Run")):
        """Blocks the CALLING thread (worker) until user picks an option."""
        result_q = queue.Queue()

        def build():
            win = tk.Toplevel(self.root)
            win.title("Checkpoint")
            win.grab_set()
            ttk.Label(win, text=message, wraplength=420, justify="left").pack(padx=16, pady=16)
            btns = ttk.Frame(win)
            btns.pack(pady=8)

            def choose(opt):
                result_q.put(opt)
                win.destroy()

            for opt in options:
                ttk.Button(btns, text=opt, command=lambda o=opt: choose(o)).pack(side="left", padx=6)
            win.protocol("WM_DELETE_WINDOW", lambda: choose(options[-1]))

        self.root.after(0, build)
        return result_q.get()

    def review_jobs(self, jobs):
        """
        jobs: list of (title, url).
        Returns list of selected urls, or None if the run was cancelled.
        """
        result_q = queue.Queue()

        def build():
            win = tk.Toplevel(self.root)
            win.title(f"Review {len(jobs)} collected jobs")
            win.geometry("640x520")
            win.grab_set()

            ttk.Label(win, text="Untick jobs you don't want to apply to, then confirm:",
                      font=("Segoe UI", 10, "bold")).pack(pady=6)

            container = ttk.Frame(win)
            container.pack(fill="both", expand=True, padx=8)

            canvas = tk.Canvas(container)
            scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
            frame = ttk.Frame(canvas)
            frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=frame, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            vars_ = []
            for title, url in jobs:
                v = tk.BooleanVar(value=True)
                text = f"{title}" if title else url
                ttk.Checkbutton(frame, text=text, variable=v).pack(anchor="w", pady=1)
                vars_.append(v)

            btn_frame = ttk.Frame(win)
            btn_frame.pack(pady=8, fill="x")

            def select_all():
                for v in vars_:
                    v.set(True)

            def deselect_all():
                for v in vars_:
                    v.set(False)

            def confirm():
                selected = [url for (title, url), v in zip(jobs, vars_) if v.get()]
                result_q.put(selected)
                win.destroy()

            def cancel_run():
                result_q.put(None)
                win.destroy()

            ttk.Button(btn_frame, text="Select All", command=select_all).pack(side="left", padx=4)
            ttk.Button(btn_frame, text="Deselect All", command=deselect_all).pack(side="left", padx=4)
            ttk.Button(btn_frame, text="Cancel Entire Run", command=cancel_run).pack(side="right", padx=4)
            ttk.Button(btn_frame, text="Apply to Selected", command=confirm).pack(side="right", padx=4)
            win.protocol("WM_DELETE_WINDOW", cancel_run)

        self.root.after(0, build)
        return result_q.get()

    def ask_answer(self, question_text):
        """Pops a text-entry dialog, blocks worker thread until answered."""
        result_q = queue.Queue()

        def build():
            answer = simpledialog.askstring(
                "Unknown screening question",
                f"The bot found a question it doesn't have an answer for:\n\n"
                f"{question_text}\n\nType your answer (leave blank to skip this field):",
                parent=self.root)
            result_q.put(answer)

        self.root.after(0, build)
        return result_q.get()


# ================================================================
# BROWSER / LOGIN / URL BUILDING (mostly unchanged from original)
# ================================================================
def create_edge_driver():
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    # options.add_argument("--headless=new")

    if WEBDRIVER_MANAGER_AVAILABLE:
        logger.info("Using webdriver-manager to auto-download EdgeDriver...")
        service = EdgeService(EdgeChromiumDriverManager().install())
    elif EDGE_DRIVER_PATH:
        logger.info(f"Using EdgeDriver at: {EDGE_DRIVER_PATH}")
        service = EdgeService(executable_path=EDGE_DRIVER_PATH)
    else:
        logger.info("No driver path specified; assuming EdgeDriver is in PATH...")
        service = EdgeService()

    return webdriver.Edge(service=service, options=options)


def login_naukri(driver, panel: ControlPanel):
    panel.log("Logging in to Naukri.com...")
    driver.get('https://login.naukri.com/')
    time.sleep(3)

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, 'usernameField'))
    )

    driver.find_element(By.ID, 'usernameField').send_keys(NAUKRI_EMAIL)
    passwd = driver.find_element(By.ID, 'passwordField')
    passwd.send_keys(NAUKRI_PASSWORD)
    passwd.send_keys(Keys.ENTER)

    time.sleep(8)
    panel.log("Login completed.")


def build_search_urls():
    urls = []
    for keyword in KEYWORDS:
        keyword_slug = keyword.lower().replace(' ', '-')
        for page_num in range(1, PAGES_PER_KEYWORD + 1):
            if not LOCATION:
                url = f"https://www.naukri.com/{keyword_slug}-jobs" if page_num == 1 \
                    else f"https://www.naukri.com/{keyword_slug}-jobs-{page_num}"
            else:
                location_slug = LOCATION.lower().replace(' ', '-')
                url = f"https://www.naukri.com/{keyword_slug}-jobs-in-{location_slug}" if page_num == 1 \
                    else f"https://www.naukri.com/{keyword_slug}-jobs-in-{location_slug}-{page_num}"
            urls.append((keyword, url))
    return urls


def open_tabs_parallel(driver, search_urls, panel):
    original_window = driver.current_window_handle
    if not search_urls:
        return original_window

    keyword, first_url = search_urls[0]
    panel.log(f"[Tab: Main] Opening: {first_url}")
    driver.get(first_url)
    time.sleep(3)

    for keyword, url in search_urls[1:]:
        panel.log(f"[Tab: New] Opening: {url}")
        driver.switch_to.new_window('tab')
        driver.get(url)
        time.sleep(2)

    driver.switch_to.window(original_window)
    return original_window


def collect_job_links_from_tab(driver, window_handle, panel):
    """Returns list of (title, url) tuples."""
    results = []
    try:
        driver.switch_to.window(window_handle)
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, 'html5lib')
        job_wrappers = soup.find_all('div', class_='srp-jobtuple-wrapper')
        if not job_wrappers:
            job_wrappers = soup.find_all('div', class_='cust-job-tuple')

        panel.log(f"[Tab: {driver.title[:50]}] Found {len(job_wrappers)} job cards")

        for job_wrapper in job_wrappers:
            title_link = job_wrapper.find('a', class_='title')
            if title_link and title_link.get('href'):
                href = title_link.get('href')
                if href.startswith('/'):
                    href = 'https://www.naukri.com' + href
                title = title_link.get_text(strip=True) or href
                results.append((title, href))

    except WebDriverException as e:
        panel.log(f"[Tab] Error reading tab: {e}")

    return results


def collect_all_jobs_parallel(driver, search_urls, panel):
    panel.log(f"Opening {len(search_urls)} search pages in parallel tabs...")
    original_window = open_tabs_parallel(driver, search_urls, panel)

    all_results = []
    window_handles = driver.window_handles
    panel.log(f"Collecting job links from {len(window_handles)} tabs...")

    for handle in window_handles:
        all_results.extend(collect_job_links_from_tab(driver, handle, panel))

    panel.log("Closing search tabs...")
    for handle in window_handles:
        if handle != original_window:
            try:
                driver.switch_to.window(handle)
                driver.close()
            except WebDriverException:
                pass
    driver.switch_to.window(original_window)

    seen = set()
    unique_results = []
    for title, url in all_results:
        if url not in seen:
            seen.add(url)
            unique_results.append((title, url))

    panel.log(f"Total unique job links collected: {len(unique_results)}")
    return unique_results


def collect_job_links_from_current_page(driver, panel):
    """
    Used by helpMe mode. Scans whatever page the user has manually navigated
    to (recommended jobs, 'you might like', saved jobs, search results,
    etc.) instead of the keyword-driven multi-tab search. Naukri uses
    different DOM structures on different pages, so this tries known job-card
    classes first, then falls back to a generic href-pattern match so it
    still works on pages we haven't specifically coded for.
    """
    results = []
    seen = set()
    try:
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, 'html5lib')

        job_wrappers = soup.find_all('div', class_='srp-jobtuple-wrapper')
        if not job_wrappers:
            job_wrappers = soup.find_all('div', class_='cust-job-tuple')

        for jw in job_wrappers:
            title_link = jw.find('a', class_='title')
            if title_link and title_link.get('href'):
                href = title_link['href']
                if href.startswith('/'):
                    href = 'https://www.naukri.com' + href
                if href not in seen:
                    seen.add(href)
                    results.append((title_link.get_text(strip=True) or href, href))

        if not results:
            # Generic fallback: any link that looks like a Naukri job detail
            # page URL, regardless of which page layout it's embedded in.
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'job-listings' in href or re.search(r'/job/\d', href):
                    if href.startswith('/'):
                        href = 'https://www.naukri.com' + href
                    if href not in seen:
                        seen.add(href)
                        title = a.get_text(strip=True) or href
                        results.append((title, href))
            if results:
                panel.log("Used fallback link-pattern matching for this page layout.")

        panel.log(f"Scan: found {len(results)} job link(s) on this page.")

    except WebDriverException as e:
        panel.log(f"Error scanning current page: {e}")

    return results


# ================================================================
# APPLY-TIME HELPERS
# ================================================================
def click_apply_button(driver, link):
    time.sleep(3)
    try:
        driver.find_element(By.ID, "company-site-button")
        return "COMPANY_SITE"
    except NoSuchElementException:
        pass

    apply_selectors = [
        (By.XPATH, "//button[contains(text(),'Apply')]"),
        (By.CSS_SELECTOR, "[class*='apply-button-container'] button"),
    ]
    for by, selector in apply_selectors:
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, selector)))
            btn.click()
            return "EASY_APPLY"
        except TimeoutException:
            pass

    return "NOT_FOUND"


def save_company_site_job(driver, naukri_url, panel):
    old_windows = driver.window_handles
    btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, "company-site-button")))
    btn.click()

    WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(old_windows))
    new_window = [w for w in driver.window_handles if w not in old_windows][0]
    driver.switch_to.window(new_window)
    WebDriverWait(driver, 10).until(lambda d: d.current_url != "about:blank")

    company_url = driver.current_url
    try:
        title = driver.find_element(By.TAG_NAME, "h1").text
    except NoSuchElementException:
        title = ""
    try:
        company = driver.find_element(By.CSS_SELECTOR, "a.comp-name").text
    except NoSuchElementException:
        company = ""

    df = pd.DataFrame([{
        "Company": company, "Job Title": title,
        "Company URL": company_url, "Naukri URL": naukri_url
    }])
    csv = "company_site_jobs.csv"
    df.to_csv(csv, mode="a" if os.path.exists(csv) else "w",
              header=not os.path.exists(csv), index=False)

    driver.close()
    driver.switch_to.window(old_windows[0])
    panel.log(f"Saved company site: {company_url}")


def find_unanswered_questions(driver):
    """
    Heuristic scan for visible, empty text/number inputs that look like
    screening questions (Naukri chatbot-style forms). Returns list of
    (element, question_text). Selectors here are a best-effort starting
    point - Naukri's DOM for these varies, adjust if it misses fields.
    """
    found = []
    try:
        inputs = driver.find_elements(
            By.CSS_SELECTOR,
            "input[type='text']:not([readonly]), input[type='number']:not([readonly]), textarea"
        )
        for el in inputs:
            try:
                if not el.is_displayed() or el.get_attribute("value"):
                    continue
                label_text = (
                    el.get_attribute("aria-label")
                    or el.get_attribute("placeholder")
                    or ""
                )
                if not label_text:
                    # try a nearby label element
                    try:
                        label_el = el.find_element(By.XPATH, "./preceding::label[1]")
                        label_text = label_el.text
                    except NoSuchElementException:
                        label_text = "(unlabeled field)"
                found.append((el, label_text))
            except WebDriverException:
                continue
    except WebDriverException:
        pass
    return found


def handle_post_apply_fields(driver, panel):
    """Original inline-form handling (first/last name, submit, quota check)."""
    try:
        driver.find_element(By.XPATH, "//*[text()='Your daily quota has been expired.']")
        panel.log("Daily quota expired.")
        return "QUOTA_EXPIRED"
    except NoSuchElementException:
        pass

    try:
        el = driver.find_element(By.ID, 'CUSTOM-FIRSTNAME')
        el.clear()
        el.send_keys(FIRSTNAME)
        panel.log("Filled custom first name field")
    except NoSuchElementException:
        pass

    try:
        el = driver.find_element(By.ID, 'CUSTOM-LASTNAME')
        el.clear()
        el.send_keys(LASTNAME)
        panel.log("Filled custom last name field")
    except NoSuchElementException:
        pass

    try:
        driver.find_element(By.XPATH, "//*[text()='Submit and Apply']").click()
        panel.log("Clicked 'Submit and Apply'")
        time.sleep(2)
    except NoSuchElementException:
        pass

    return "OK"


def lookup_answer(question_text):
    """
    Substring match against ANSWER_BANK, since real form labels are full
    sentences ("What is your notice period?") not bare keys ("notice period").
    Picks the LONGEST matching key so specific keys win over vague ones
    (e.g. "expected ctc" wins over "ctc" if both were present).
    """
    q = question_text.strip().lower()
    best_key = None
    for key in ANSWER_BANK:
        if key.lower() in q:
            if best_key is None or len(key) > len(best_key):
                best_key = key
    return ANSWER_BANK.get(best_key) if best_key else None


def scan_and_continue(driver, panel: ControlPanel):
    """
    Called right after the user hits Resume, while a job's page is actually
    open in the browser. Drives THIS page to completion instead of just
    peeking at it:
      1. company-site redirect -> save it
      2. Apply button not yet clicked -> click it
      3. any visible unanswered question fields -> answer from ANSWER_BANK,
         or ask via popup, then try Submit again (repeats a few passes since
         answering one field can reveal another)
      4. Submit button visible -> click it
    Returns (action, counted_as) where counted_as is "applied", "failed",
    or None (nothing to count, e.g. company-site jobs are saved separately).
    action is always "advance" - the caller moves on to the next job.
    """
    counted_as = None
    try:
        # 1) Company-site redirect
        try:
            driver.find_element(By.ID, "company-site-button")
            save_company_site_job(driver, driver.current_url, panel)
            panel.log("Scan: company-site job - saved and moving on.")
            return "advance", None
        except NoSuchElementException:
            pass

        # 2) Apply button not yet clicked?
        apply_selectors = [
            (By.XPATH, "//button[contains(text(),'Apply')]"),
            (By.CSS_SELECTOR, "[class*='apply-button-container'] button"),
        ]
        for by, sel in apply_selectors:
            try:
                btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel)))
                btn.click()
                counted_as = "applied"
                panel.log("Scan: found an un-clicked Apply button - clicked it.")
                time.sleep(2)
                break
            except TimeoutException:
                continue

        # 3) Answer any visible screening questions (a few passes - answering
        #    one field can reveal the next one)
        for _ in range(3):
            unanswered = find_unanswered_questions(driver)
            if not unanswered:
                break
            panel.log(f"Scan: found {len(unanswered)} unanswered field(s).")
            for el, question_text in unanswered:
                answer = lookup_answer(question_text)
                if not answer:
                    answer = panel.ask_answer(question_text)
                if answer:
                    try:
                        el.clear()
                        el.send_keys(answer)
                        panel.log(f"Answered '{question_text[:50]}' -> {answer}")
                    except WebDriverException:
                        panel.log(f"Could not fill field for: {question_text[:50]}")
                else:
                    panel.log(f"Skipped (no answer given): {question_text[:50]}")
            if counted_as is None:
                counted_as = "applied"

        # 4) Submit if a submit button is present now
        submit_selectors = [
            (By.XPATH, "//*[text()='Submit and Apply']"),
            (By.XPATH, "//button[contains(translate(text(),"
                       "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]"),
        ]
        for by, sel in submit_selectors:
            try:
                btn = driver.find_element(by, sel)
                btn.click()
                panel.log("Scan: clicked Submit.")
                time.sleep(2)
                if counted_as is None:
                    counted_as = "applied"
                break
            except NoSuchElementException:
                continue

        if counted_as is None:
            panel.log("Scan: nothing to apply/submit found on this page.")
            counted_as = "failed"

        return "advance", counted_as

    except Exception as e:
        panel.log(f"Scan error: {e} - moving to next job.")
        return "advance", counted_as


def wait_while_paused_or_manual(control: ControlState, panel: ControlPanel):
    """Blocks here whenever state is PAUSED or MANUAL. Returns when RUNNING again."""
    while control.get_state() != ControlState.RUNNING:
        check_shutdown(control)
        state = control.get_state()
        if state == ControlState.PAUSED:
            panel.set_status("PAUSED - click Resume, Stop, or Cancel Job")
        elif state == ControlState.MANUAL:
            panel.set_status("MANUAL CONTROL - do what you need in the browser, "
                              "then click Resume")
        control.resume_event.wait(timeout=0.3)
    check_shutdown(control)


# ================================================================
# MAIN APPLY LOOP - pause/resume/cancel aware
# ================================================================
def apply_to_jobs(driver, job_links, control: ControlState, panel: ControlPanel):
    applied = 0
    failed = 0
    applied_list = {'passed': [], 'failed': []}
    total = len(job_links)
    i = 0

    while i < total:
        check_shutdown(control)

        if applied >= MAX_APPLICATIONS:
            panel.log(f"Reached max application limit ({MAX_APPLICATIONS}). Stopping.")
            break

        # Pause point: before opening the next job. No job page is loaded
        # yet at this point (whatever's on screen belongs to the PREVIOUS,
        # already-finished job) - so on resume we just continue normally,
        # we do NOT scan here (scanning here was the bug: it inspected a
        # stale finished page and skipped the upcoming job entirely).
        if control.get_state() != ControlState.RUNNING:
            wait_while_paused_or_manual(control, panel)
            control.rescan_on_resume = False  # nothing to scan yet - discard

        if control.request_cancel_job:
            control.request_cancel_job = False
            panel.log("Job cancelled before opening. Skipping.")
            i += 1
            continue

        link = job_links[i]
        panel.set_status(f"[{i + 1}/{total}] Opening job")
        panel.log(f"[{i + 1}/{total}] Visiting: {link}")

        try:
            driver.get(link)
        except WebDriverException as e:
            panel.log(f"Failed to load page: {e}")
            failed += 1
            applied_list['failed'].append(link)
            i += 1
            continue

        # Pause point: right after page load, before clicking Apply. This IS
        # where a resume should scan-and-complete, since the job's own page
        # is genuinely the one on screen.
        if control.get_state() != ControlState.RUNNING:
            wait_while_paused_or_manual(control, panel)
            if control.rescan_on_resume:
                control.rescan_on_resume = False
                outcome, counted_as = scan_and_continue(driver, panel)
                if counted_as == "applied":
                    applied += 1
                    applied_list["passed"].append(link)
                elif counted_as == "failed":
                    failed += 1
                    applied_list["failed"].append(link)
                i += 1
                continue

        if control.request_cancel_job:
            control.request_cancel_job = False
            panel.log("Job cancelled after opening. Skipping.")
            failed += 1
            applied_list['failed'].append(link)
            i += 1
            continue

        result = click_apply_button(driver, link)

        if result == "COMPANY_SITE":
            save_company_site_job(driver, link, panel)
            i += 1
            continue
        if result == "NOT_FOUND":
            failed += 1
            applied_list["failed"].append(link)
            i += 1
            continue

        applied += 1
        applied_list["passed"].append(link)

        outcome = handle_post_apply_fields(driver, panel)
        if outcome == "QUOTA_EXPIRED":
            break

        i += 1

    return applied, failed, applied_list



def save_results(applied_list, panel, append=False):
    csv_file = "naukriapplied.csv"
    final_dict = {k: pd.Series(v) for k, v in applied_list.items()}
    df = pd.DataFrame.from_dict(final_dict)
    if append and os.path.exists(csv_file):
        df.to_csv(csv_file, mode="a", header=False, index=False)
    else:
        df.to_csv(csv_file, index=False)
    panel.log(f"Results saved to {csv_file}")


# ================================================================
# WORKER THREAD - runs the whole automation, GUI stays responsive
# ================================================================
def run_worker(control: ControlState, panel: ControlPanel):
    driver = None
    try:
        panel.log("Launching Edge browser...")
        driver = create_edge_driver()

        login_naukri(driver, panel)
        check_shutdown(control)

        search_urls = build_search_urls()
        panel.set_status("Ready to collect jobs")

        # ---- CHECKPOINT 1: before collecting ----
        choice = panel.confirm_checkpoint(
            f"About to search {len(KEYWORDS)} keyword(s) "
            f"({', '.join(KEYWORDS)}) across {PAGES_PER_KEYWORD} page(s) each "
            f"= {len(search_urls)} search pages total.\n\nProceed to collect job links?",
            options=("Continue", "Cancel Run")
        )
        if choice != "Continue":
            panel.log("Run cancelled before collection.")
            panel.set_status("Cancelled.")
            return
        check_shutdown(control)

        panel.set_status("Collecting job links...")
        job_items = collect_all_jobs_parallel(driver, search_urls, panel)
        check_shutdown(control)

        if not job_items:
            panel.log("No job links found. Check keywords/location.")
            panel.info("No jobs found. Check your keywords and location.")
            panel.set_status("No jobs found.")
            return

        # ---- CHECKPOINT 2: review & prune ----
        selected_urls = panel.review_jobs(job_items)
        check_shutdown(control)
        if selected_urls is None:
            panel.log("Run cancelled at review step.")
            panel.set_status("Cancelled.")
            return
        if not selected_urls:
            panel.log("No jobs selected. Ending run.")
            panel.set_status("No jobs selected.")
            return

        panel.log(f"{len(selected_urls)} job(s) selected for application.")
        panel.set_status(f"Applying to {len(selected_urls)} job(s)...")

        applied, failed, applied_list = apply_to_jobs(driver, selected_urls, control, panel)
        save_results(applied_list, panel)

        panel.set_status(f"Done. Applied: {applied}  Failed: {failed}")
        panel.log("=" * 40)
        panel.log(f"APPLIED: {applied}  FAILED: {failed}  TOTAL: {applied + failed}")
        panel.log("=" * 40)

    except ShutdownRequested:
        panel.log("Shutdown requested. Stopping worker.")
        panel.set_status("Stopped by user.")
    except Exception as e:
        panel.log(f"ERROR: {e}")
        logger.exception(e)
        panel.set_status("Error - see log.")
    finally:
        panel.log("Browser left open (driver.quit() intentionally skipped, as before).")
        panel.log(">> Run finished - Pause/Resume/Stop/Cancel are now inactive "
                  "(nothing left running to control).")
        panel.finish()


def run_worker_helpme(control: ControlState, panel: ControlPanel):
    """
    'helpMe' mode: log in, then wait. The user drives the browser to
    whatever page they want (recommended jobs, search results, saved jobs,
    'you might like', etc). Each time they click 'Scan This Page & Apply
    All', the CURRENT page is scraped, a review checklist pops up, and
    apply_to_jobs runs on whatever they confirm. Can be repeated on
    different pages until Quit.
    """
    driver = None
    first_batch = True
    try:
        panel.log("Launching Edge browser (helpMe mode)...")
        driver = create_edge_driver()

        login_naukri(driver, panel)
        check_shutdown(control)

        panel.set_status("Ready - browse to any job list page, then click "
                          "'Scan This Page & Apply All'.")
        panel.log("Navigate anywhere on Naukri.com in the browser window. "
                   "When jobs are visible, click 'Scan This Page & Apply All' below.")
        panel.enable_scan_button()

        while True:
            check_shutdown(control)
            try:
                signal = control.scan_queue.get(timeout=0.3)
            except queue.Empty:
                continue

            if signal == "QUIT":
                break

            if signal == "SCAN":
                panel.set_status("Scanning current page for job links...")
                job_items = collect_job_links_from_current_page(driver, panel)

                if not job_items:
                    panel.info("No job links found on the current page.")
                    panel.set_status("Ready - browse & click Scan again.")
                    continue

                selected_urls = panel.review_jobs(job_items)
                check_shutdown(control)

                if not selected_urls:
                    panel.log("No jobs selected for this batch.")
                    panel.set_status("Ready - browse & click Scan again.")
                    continue

                panel.log(f"{len(selected_urls)} job(s) selected. Applying...")
                panel.set_status(f"Applying to {len(selected_urls)} job(s)...")

                applied, failed, applied_list = apply_to_jobs(driver, selected_urls, control, panel)
                save_results(applied_list, panel, append=not first_batch)
                first_batch = False

                panel.log(f"Batch done. Applied: {applied}  Failed: {failed}")
                panel.set_status("Ready - browse to another page & click Scan again, or Quit.")

    except ShutdownRequested:
        panel.log("Shutdown requested. Stopping worker.")
        panel.set_status("Stopped by user.")
    except Exception as e:
        panel.log(f"ERROR: {e}")
        logger.exception(e)
        panel.set_status("Error - see log.")
    finally:
        panel.log("Browser left open (driver.quit() intentionally skipped, as before).")
        panel.finish()


def main():
    if not validate_config():
        return

    help_me_mode = len(sys.argv) > 1 and sys.argv[1] == "helpMe"

    control = ControlState()
    panel = ControlPanel(control)

    target = run_worker_helpme if help_me_mode else run_worker
    worker = threading.Thread(target=target, args=(control, panel), daemon=True)
    panel.root.after(400, worker.start)
    panel.root.mainloop()


if __name__ == "__main__":
    main()