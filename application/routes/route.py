"""Core HTML routes for Portfolio Manager."""
import uuid
import csv
import io
from functools import wraps

from flask import (render_template, request, redirect, session, url_for,
                   jsonify, flash)
from werkzeug.security import generate_password_hash, check_password_hash
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

from application import app
from application.services.azure_table import (user_table_client,
                                              stocks_table_client)
from application.services import verification
from application.services import precompute
from application.constants import PERSONAS, get_persona


# ---------- Helpers ----------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "email" not in session or "user_id" not in session:
            return redirect(url_for("logIn"))
        return view(*args, **kwargs)
    return wrapped


# Endpoints a logged-in user may reach before choosing a persona.
_PERSONA_EXEMPT_ENDPOINTS = {
    "choose_persona", "set_persona", "logout", "static",
    "verify_email", "verify_email_resend", "verify_email_cancel",
}


@app.before_request
def _require_persona():
    """Route logged-in users without a persona to the onboarding page.

    Only applies to top-level HTML page navigations (GET requests that
    accept HTML) so JSON/API/polling endpoints keep working normally.
    """
    if not session.get("user_id") or session.get("persona"):
        return None
    if request.method != "GET":
        return None
    endpoint = request.endpoint or ""
    if endpoint in _PERSONA_EXEMPT_ENDPOINTS:
        return None
    # Skip blueprint API endpoints and anything that isn't asking for HTML.
    if "." in endpoint or "text/html" not in (request.headers.get("Accept") or ""):
        return None
    return redirect(url_for("choose_persona"))


def _clean_value(val):
    """Unwrap Azure Table EntityProperty / dict values into plain Python values."""
    if val is None:
        return None
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, dict) and "_" in val:
        return val["_"]
    return val


def _clean_stock(raw):
    stock = dict(raw)
    for key in ("Quantity", "PurchasePrice", "CurrentPrice", "PreviousClose"):
        if key in stock:
            stock[key] = _clean_value(stock[key])
    # Coerce numerics
    try:
        stock["Quantity"] = int(stock.get("Quantity") or 0)
    except (TypeError, ValueError):
        stock["Quantity"] = 0
    for k in ("PurchasePrice", "CurrentPrice", "PreviousClose"):
        try:
            stock[k] = float(stock.get(k) or 0)
        except (TypeError, ValueError):
            stock[k] = 0.0
    return stock


def _verify_password(stored, provided):
    """Support both legacy plaintext and new hashed passwords."""
    if not stored:
        return False
    if stored.startswith(("pbkdf2:", "scrypt:", "argon2:")):
        try:
            return check_password_hash(stored, provided)
        except Exception:
            return False
    return stored == provided


def _fetch_user_by_email(email):
    norm = (email or "").strip().lower()
    if not norm:
        return None
    # Fast path: exact match. New accounts store the email lowercased, so
    # this hits for almost everyone. Escape single quotes to keep the OData
    # filter safe.
    safe = norm.replace("'", "''")
    users = list(user_table_client.query_entities(
        query_filter=f"Email eq '{safe}'"))
    if users:
        return users[0]
    # Fallback: case-insensitive scan for legacy accounts whose email was
    # stored with mixed case (an exact, case-sensitive match would miss them).
    try:
        for u in user_table_client.query_entities(
                query_filter="PartitionKey eq 'user'"):
            if str(u.get("Email", "")).strip().lower() == norm:
                return u
    except Exception as e:
        print(f"[user] case-insensitive lookup failed: {e}")
    return None


def _persist_user_persona(email, persona_id):
    """Save the chosen persona on the user's Azure Table entity."""
    try:
        user = _fetch_user_by_email(email)
        if user:
            user["Persona"] = persona_id
            user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
    except Exception as e:  # non-fatal: session still drives the UI
        print(f"[persona] could not persist persona: {e}")


def _fetch_user_stocks(user_id):
    try:
        items = list(stocks_table_client.query_entities(
            query_filter=f"UserId eq '{user_id}'"))
    except ResourceNotFoundError:
        return []
    except Exception as e:
        print(f"[stocks] fetch error: {e}")
        return []
    return [_clean_stock(s) for s in items]


# ---------- Auth ----------

@app.route("/")
def index():
    if "email" in session:
        return redirect(url_for("home"))
    return render_template("landing.html")


@app.route("/favicon.ico")
def favicon():
    # Browsers auto-request /favicon.ico; serve the brand logo from /static.
    return redirect(url_for("static", filename="logo.png"))


@app.route("/login", methods=["GET", "POST"])
def logIn():
    if "email" in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or not password:
            return render_template("login.html",
                                   error="Email and password are required.")
        try:
            user = _fetch_user_by_email(email)
            if user and _verify_password(user.get("Password"), password):
                # Email-verification gate (legacy users without the field
                # are auto-verified inside verification.is_user_verified).
                if not verification.is_user_verified(user):
                    session["pending_verify_email"] = user.get("Email", email)
                    session["pending_verify_user_id"] = user.get("RowKey", "")
                    session["pending_verify_name"] = user.get("UserName", "")
                    # Send a fresh code
                    verification.start(
                        email=user.get("Email", email),
                        user_id=user.get("RowKey", ""),
                        name=user.get("UserName", ""))
                    flash("Please verify your email to continue. A code has been sent. "
                          "Don't see it? Check your Spam / Promotions folder.", "warning")
                    return redirect(url_for("verify_email"))
                session["name"] = user.get("UserName", "User")
                session["email"] = user.get("Email", email)
                session["user_id"] = user.get("RowKey", "")
                session["persona"] = (user.get("Persona") or "") or None
                session["just_logged_in"] = True
                return redirect(url_for("home"))
            return render_template("login.html",
                                   error="Invalid email or password.")
        except ResourceNotFoundError:
            return render_template("login.html",
                                   error="Service unavailable. Try again later.")
        except Exception as e:
            print(f"[login] error: {e}")
            return render_template("login.html",
                                   error="An unexpected error occurred.")
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "email" in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        gender = request.form.get("gender") or ""
        location = (request.form.get("location") or "").strip()
        password = request.form.get("password") or ""

        if not all([name, email, phone, password]):
            return render_template("signup.html",
                                   error="Please fill in all required fields.")
        if len(password) < 6:
            return render_template("signup.html",
                                   error="Password must be at least 6 characters.")
        try:
            if _fetch_user_by_email(email):
                return render_template("signup.html",
                                       error="An account with that email already exists.")
            entity = {
                "PartitionKey": "user",
                "RowKey": str(uuid.uuid4()),
                "UserName": name,
                "Email": email,
                "ContactNo": phone,
                "Gender": gender,
                "Location": location,
                "Password": generate_password_hash(password),
                "EmailVerified": False,
                "Plan": "free",
                "PlanExpiresOn": "",
                "TrialUsed": False,
            }
            # Activate 7-day Elite trial for the new user.
            from application.services.plans import start_trial
            start_trial(entity)
            user_table_client.create_entity(entity=entity)
            # Stash pending state and trigger OTP. Do NOT log the user in yet.
            session["pending_verify_email"] = email
            session["pending_verify_user_id"] = entity["RowKey"]
            session["pending_verify_name"] = name
            ok, info = verification.start(
                email=email, user_id=entity["RowKey"], name=name)
            if not ok:
                # Email failed; tell the user but keep them in the verify flow
                flash("We saved your account but couldn't send the verification code. "
                      "Try the Resend button. (" + info + ")", "danger")
            else:
                flash("We sent a 6-digit code to your email. "
                      "Don't see it? Check your Spam / Promotions folder.",
                      "info")
            return redirect(url_for("verify_email"))
        except ResourceNotFoundError:
            return render_template("signup.html",
                                   error="Service unavailable. Try again later.")
        except Exception as e:
            print(f"[signup] error: {e}")
            return render_template("signup.html",
                                   error=f"Could not create account: {e}")
    return render_template("signup.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("logIn"))


# ---------- Email verification ----------

@app.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    """OTP verification page. Used after signup or for unverified login."""
    pending_email = session.get("pending_verify_email")
    if not pending_email:
        # Nothing to verify — kick back to login
        return redirect(url_for("logIn"))

    if request.method == "POST":
        code = (request.form.get("code") or "").strip().replace(" ", "")
        ok, info = verification.verify(pending_email, code)
        if ok:
            # Promote the pending user to a real session
            session["name"] = session.pop("pending_verify_name", "User")
            session["email"] = session.pop("pending_verify_email")
            session["user_id"] = session.pop("pending_verify_user_id", "")
            session["persona"] = None
            session["just_logged_in"] = True
            flash("Email verified \u2014 welcome aboard!", "success")
            return redirect(url_for("home"))
        return render_template("verifyEmail.html",
                               email=pending_email, error=info)

    return render_template("verifyEmail.html", email=pending_email)


@app.route("/verify-email/resend", methods=["POST"])
def verify_email_resend():
    """JSON resend endpoint with cooldown."""
    pending_email = session.get("pending_verify_email")
    if not pending_email:
        return jsonify({"ok": False, "error": "no pending verification"}), 400
    ok, info = verification.resend(pending_email)
    return jsonify({"ok": ok, "message": info}), (200 if ok else 429)


@app.route("/verify-email/cancel")
def verify_email_cancel():
    """Abandon a pending verification (e.g. user wants to sign up again)."""
    for k in ("pending_verify_email", "pending_verify_user_id",
              "pending_verify_name"):
        session.pop(k, None)
    return redirect(url_for("logIn"))


# ---------- Forgot / reset password ----------

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Step 1: collect the account email and send a one-time code to it."""
    if "email" in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not email:
            return render_template("forgotPassword.html",
                                   error="Please enter your email address.")
        try:
            user = _fetch_user_by_email(email)
        except Exception as e:
            print(f"[forgot] lookup error: {e}")
            user = None
        # Only actually send when the account exists, but always respond the
        # same way so we don't reveal which emails are registered.
        if user:
            verification.start(
                email=user.get("Email", email),
                user_id=user.get("RowKey", ""),
                name=user.get("UserName", ""))
        session["pw_reset_email"] = email
        session.pop("pw_reset_verified", None)
        flash("If an account exists for that email, a 6-digit code has been "
              "sent. Don't see it? Check your Spam / Promotions folder.", "info")
        return redirect(url_for("reset_password"))

    return render_template("forgotPassword.html")


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    """Step 2: verify the OTP, then (step 3) set a new password."""
    email = session.get("pw_reset_email")
    if not email:
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "verify":
            code = (request.form.get("code") or "").strip().replace(" ", "")
            ok, info = verification.verify(email, code)
            if ok:
                session["pw_reset_verified"] = True
                return render_template("resetPassword.html", email=email,
                                       stage="password")
            return render_template("resetPassword.html", email=email,
                                   stage="otp", error=info)

        if action == "password":
            if not session.get("pw_reset_verified"):
                return render_template("resetPassword.html", email=email,
                                       stage="otp",
                                       error="Please verify the code first.")
            new = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if len(new) < 6:
                return render_template("resetPassword.html", email=email,
                                       stage="password",
                                       error="Password must be at least 6 characters.")
            if new != confirm:
                return render_template("resetPassword.html", email=email,
                                       stage="password",
                                       error="Password confirmation does not match.")
            try:
                user = _fetch_user_by_email(email)
                if not user:
                    raise ValueError("account not found")
                user["Password"] = generate_password_hash(new)
                user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
            except Exception as e:
                print(f"[reset] update error: {e}")
                return render_template("resetPassword.html", email=email,
                                       stage="password",
                                       error="Could not update the password. "
                                             "Please try again.")
            for k in ("pw_reset_email", "pw_reset_verified"):
                session.pop(k, None)
            flash("Your password has been updated. Please sign in.", "success")
            return redirect(url_for("logIn"))

    stage = "password" if session.get("pw_reset_verified") else "otp"
    return render_template("resetPassword.html", email=email, stage=stage)


@app.route("/reset-password/resend", methods=["POST"])
def reset_password_resend():
    """JSON resend endpoint for the password-reset OTP (with cooldown)."""
    email = session.get("pw_reset_email")
    if not email:
        return jsonify({"ok": False, "error": "no reset in progress"}), 400
    ok, info = verification.resend(email)
    return jsonify({"ok": ok, "message": info}), (200 if ok else 429)


@app.route("/reset-password/cancel")
def reset_password_cancel():
    """Abandon a password reset in progress."""
    for k in ("pw_reset_email", "pw_reset_verified"):
        session.pop(k, None)
    return redirect(url_for("logIn"))


# ---------- Pages ----------

@app.route("/home")
@login_required
def home():
    if not session.get("persona"):
        return redirect(url_for("choose_persona"))
    stocks = _fetch_user_stocks(session["user_id"])
    just_logged_in = session.pop("just_logged_in", False)
    return render_template("home.html",
                           name=session["name"], email=session["email"],
                           title="Dashboard", stocks=stocks,
                           just_logged_in=just_logged_in)


@app.route("/choose-persona")
@login_required
def choose_persona():
    """Onboarding page: pick a persona (trader / swing / investor)."""
    return render_template(
        "choosePersona.html",
        name=session.get("name"), email=session.get("email"),
        personas=PERSONAS,
        current_persona=session.get("persona"),
        title="Welcome")


@app.route("/set-persona", methods=["POST"])
@login_required
def set_persona():
    """Persist the chosen persona and tailor the sidebar accordingly.

    Used both for first-time onboarding and for switching personas later
    from the topbar. Honours an optional ``next`` redirect target.
    """
    persona_id = (request.form.get("persona") or "").strip().lower()
    if not get_persona(persona_id):
        flash("Please choose a valid profile to continue.", "warning")
        return redirect(url_for("choose_persona"))

    session["persona"] = persona_id
    _persist_user_persona(session["email"], persona_id)

    # Only allow internal redirects to avoid open-redirect issues.
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("home"))


@app.route("/portfolioAnalysis")
@login_required
def portfolioAnalysis():
    stocks = _fetch_user_stocks(session["user_id"])
    return render_template("portfolioAnalysis.html",
                           name=session["name"], email=session["email"],
                           title="Portfolio Analysis", stocks=stocks)


@app.route("/algoHelper")
@login_required
def algoHelper():
    stocks = _fetch_user_stocks(session["user_id"])
    return render_template("algoHelper.html",
                           name=session["name"], email=session["email"],
                           title="AI Assistant", stocks=stocks)


@app.route("/advanced-dashboard")
@login_required
def advanced_dashboard():
    return render_template("advancedDashboard.html",
                           name=session["name"], email=session["email"],
                           title="Advanced Metrics")


@app.route("/portfolioMaker")
@login_required
def portfolioMaker():
    stocks = _fetch_user_stocks(session["user_id"])
    return render_template("portfolioMaker.html",
                           name=session["name"], email=session["email"],
                           stocks=stocks,
                           title="Add Stock")


@app.route("/profile")
@login_required
def profile_page():
    user = _fetch_user_by_email(session["email"]) or {}
    stocks = _fetch_user_stocks(session["user_id"])
    profile = {
        "name": user.get("UserName", session.get("name", "")),
        "email": user.get("Email", session.get("email", "")),
        "phone": user.get("ContactNo", ""),
        "gender": user.get("Gender", ""),
        "location": user.get("Location", ""),
    }
    return render_template("profile.html", user=profile, stocks=stocks,
                           name=profile["name"], email=profile["email"],
                           title="My Account")


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = _fetch_user_by_email(session["email"])
    error = success = None
    if request.method == "POST" and user:
        action = request.form.get("action")
        try:
            if action == "profile":
                user["UserName"] = request.form.get("name", user.get("UserName"))
                user["ContactNo"] = request.form.get("phone", user.get("ContactNo"))
                user["Location"] = request.form.get("location", user.get("Location"))
                user["Gender"] = request.form.get("gender", user.get("Gender"))
                user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
                session["name"] = user["UserName"]
                success = "Profile updated successfully."
            elif action == "password":
                current = request.form.get("current_password") or ""
                new = request.form.get("new_password") or ""
                confirm = request.form.get("confirm_password") or ""
                if not _verify_password(user.get("Password"), current):
                    error = "Current password is incorrect."
                elif len(new) < 6:
                    error = "New password must be at least 6 characters."
                elif new != confirm:
                    error = "Password confirmation does not match."
                else:
                    user["Password"] = generate_password_hash(new)
                    user_table_client.update_entity(entity=user, mode=UpdateMode.MERGE)
                    success = "Password changed successfully."
        except Exception as e:
            print(f"[settings] error: {e}")
            error = f"Could not save changes: {e}"
        # Refresh user
        user = _fetch_user_by_email(session["email"])

    return render_template("settings.html", user=user or {},
                           name=session["name"], email=session["email"],
                           title="Settings", error=error, success=success)


# ---------- Portfolio CRUD ----------

@app.route("/portfolio/add", methods=["POST"])
@login_required
def add_to_portfolio():
    try:
        # Plan gate — enforce holdings cap
        from application.services import plans
        plan = plans.current_plan()
        cap = plan.get("limits", {}).get("holdings")
        if cap is not None:
            current_count = len(_fetch_user_stocks(session["user_id"]))
            if current_count >= cap:
                flash(f"You've reached the {cap}-holding limit on the {plan['name']} plan. "
                      f"Upgrade to add more stocks.", "warning")
                return redirect(url_for("billing.billing_page"))

        stock_name = (request.form.get("stock_name") or "").strip()
        if not stock_name:
            flash("Stock name is required.", "danger")
            return redirect(url_for("portfolioMaker"))

        symbol = (request.form.get("symbol") or "").strip().upper()
        sector = request.form.get("sector") or ""
        try:
            purchase_price = float(request.form.get("purchase_price") or 0)
        except (TypeError, ValueError):
            purchase_price = 0.0

        # Auto-fetch current price (and fill sector when missing) via market_data
        current_price = 0.0
        if symbol:
            try:
                from application.services import market_data
                quote = market_data.get_quote(symbol) or {}
                info = market_data.get_info(symbol) or {}
                price = quote.get("price")
                if price:
                    current_price = float(price)
                if not sector and info.get("sector"):
                    sector = info["sector"]
            except Exception as e:
                print(f"[portfolio.add] price fetch failed: {e}")

        if not current_price:
            current_price = purchase_price

        entity = {
            "PartitionKey": "stock",
            "RowKey": str(uuid.uuid4()),
            "UserId": session["user_id"],
            "StockName": stock_name,
            "Quantity": int(request.form.get("quantity") or 0),
            "PurchasePrice": purchase_price,
            "CurrentPrice": float(current_price),
            "Sector": sector or "Other",
            "Symbol": symbol,
        }
        stocks_table_client.create_entity(entity=entity)
        precompute.invalidate_user(session["user_id"])
        flash("Stock added to your portfolio.", "success")
        return redirect(url_for("portfolioMaker"))
    except Exception as e:
        print(f"[portfolio.add] error: {e}")
        flash(f"Could not add stock: {e}", "danger")
        return redirect(url_for("portfolioMaker"))


# ---------- CSV Import ----------

# Normalise CSV column headers from various brokers
_COL_MAP = {
    # Stock name variants
    "stock name": "stock_name", "company": "stock_name", "company name": "stock_name",
    "name": "stock_name", "scrip name": "stock_name", "instrument": "stock_name",
    "tradingsymbol": "stock_name", "trading symbol": "stock_name",
    # Symbol
    "symbol": "symbol", "scrip code": "symbol", "isin": "symbol",
    "nse symbol": "symbol", "bse symbol": "symbol", "ticker": "symbol",
    # Quantity
    "quantity": "quantity", "qty": "quantity", "total qty": "quantity",
    "net qty": "quantity", "holding qty": "quantity", "shares": "quantity",
    # Purchase / average price
    "purchase price": "purchase_price", "buy price": "purchase_price",
    "avg price": "purchase_price", "average price": "purchase_price",
    "avg cost": "purchase_price", "cost price": "purchase_price",
    "averageprice": "purchase_price", "buy avg": "purchase_price",
    # Current / last price
    "current price": "current_price", "ltp": "current_price",
    "last price": "current_price", "market price": "current_price",
    "close price": "current_price", "closing price": "current_price",
    # Sector
    "sector": "sector", "industry": "sector",
}


def _normalise_header(h):
    return _COL_MAP.get(h.strip().lower().replace("_", " "), None)


@app.route("/portfolio/import-csv", methods=["POST"])
@login_required
def import_csv():
    """Parse an uploaded CSV/Excel-exported CSV and bulk-add stocks."""
    # Plan gate — holdings cap
    from application.services import plans
    plan = plans.current_plan()
    cap = plan.get("limits", {}).get("holdings")
    if cap is not None:
        current_count = len(_fetch_user_stocks(session["user_id"]))
        if current_count >= cap:
            return jsonify(error=f"You've reached the {cap}-holding limit on the {plan['name']} plan. "
                                  f"Upgrade at /billing to import more.",
                           upgrade_url="/billing"), 402

    f = request.files.get("csv_file")
    if not f or not f.filename:
        return jsonify(error="No file selected."), 400

    fname = f.filename.lower()
    if not fname.endswith(".csv"):
        return jsonify(error="Only .csv files are supported."), 400

    try:
        raw = f.read()
        # Try UTF-8 first, fall back to latin-1
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return jsonify(error="CSV file is empty or has no headers."), 400

        # Map CSV columns → our fields
        col_map = {}
        for original in reader.fieldnames:
            mapped = _normalise_header(original)
            if mapped:
                col_map[original] = mapped

        if "stock_name" not in col_map.values() and "symbol" not in col_map.values():
            return jsonify(
                error="Could not detect stock name or symbol column. "
                      "Expected headers like: Stock Name, Symbol, Quantity, "
                      "Buy Price, Current Price"
            ), 400

        imported = 0
        skipped = 0
        errors = []

        for i, row in enumerate(reader, start=2):
            mapped = {}
            for orig_col, our_col in col_map.items():
                mapped[our_col] = (row.get(orig_col) or "").strip()

            stock_name = mapped.get("stock_name", "")
            symbol = mapped.get("symbol", "")
            if not stock_name and not symbol:
                skipped += 1
                continue

            if not stock_name:
                stock_name = symbol

            # If symbol looks like plain text (not ending with .NS/.BO), try appending .NS
            if symbol and not symbol.upper().endswith((".NS", ".BO")):
                symbol = symbol.upper() + ".NS"
            elif symbol:
                symbol = symbol.upper()

            try:
                qty = int(float(mapped.get("quantity", "0") or "0"))
            except ValueError:
                qty = 0
            try:
                pp = float(mapped.get("purchase_price", "0") or "0")
            except ValueError:
                pp = 0.0
            try:
                cp = float(mapped.get("current_price", "0") or "0")
            except ValueError:
                cp = 0.0

            if qty <= 0:
                skipped += 1
                continue

            sector = mapped.get("sector", "") or "Other"

            entity = {
                "PartitionKey": "stock",
                "RowKey": str(uuid.uuid4()),
                "UserId": session["user_id"],
                "StockName": stock_name,
                "Quantity": qty,
                "PurchasePrice": pp,
                "CurrentPrice": cp if cp else pp,
                "Sector": sector,
                "Symbol": symbol,
            }
            try:
                stocks_table_client.create_entity(entity=entity)
                imported += 1
            except Exception as e:
                errors.append(f"Row {i}: {e}")

        if imported:
            precompute.invalidate_user(session["user_id"])
        return jsonify(imported=imported, skipped=skipped,
                       errors=errors[:5])  # cap error list

    except Exception as e:
        return jsonify(error=f"Failed to process CSV: {e}"), 500


@app.route("/portfolio/delete/<row_key>", methods=["POST"])
@login_required
def delete_from_portfolio(row_key):
    try:
        existing = stocks_table_client.get_entity(
            partition_key="stock", row_key=row_key)
        if existing.get("UserId") != session["user_id"]:
            return jsonify({"error": "Forbidden"}), 403
        stocks_table_client.delete_entity(
            partition_key="stock", row_key=row_key)
        precompute.invalidate_user(session["user_id"])
        return jsonify({"ok": True})
    except ResourceNotFoundError:
        # Try with old PartitionKey value used by legacy data
        try:
            stocks_table_client.delete_entity(
                partition_key="PartitionKey", row_key=row_key)
            precompute.invalidate_user(session["user_id"])
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 404
    except Exception as e:
        print(f"[portfolio.delete] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/portfolio/update/<row_key>", methods=["POST"])
@login_required
def update_portfolio_stock(row_key):
    try:
        data = request.get_json(silent=True) or request.form
        for pk in ("stock", "PartitionKey"):
            try:
                entity = stocks_table_client.get_entity(
                    partition_key=pk, row_key=row_key)
                break
            except ResourceNotFoundError:
                entity = None
        if not entity:
            return jsonify({"error": "Not found"}), 404
        if entity.get("UserId") != session["user_id"]:
            return jsonify({"error": "Forbidden"}), 403

        for src, dst, cast in [
            ("quantity", "Quantity", int),
            ("purchase_price", "PurchasePrice", float),
            ("current_price", "CurrentPrice", float),
            ("purchase_date", "PurchaseDate", str),
            ("sector", "Sector", str),
            ("exchange", "Exchange", str),
            ("symbol", "Symbol", str),
        ]:
            if src in data and data[src] not in (None, ""):
                try:
                    entity[dst] = cast(data[src])
                except (TypeError, ValueError):
                    pass
        stocks_table_client.update_entity(entity=entity, mode=UpdateMode.MERGE)
        precompute.invalidate_user(session["user_id"])
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[portfolio.update] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/portfolio/refresh-prices", methods=["POST"])
@login_required
def refresh_prices():
    """Refresh CurrentPrice for any stock with a Symbol set, via market_data."""
    from application.services import market_data
    updated = 0
    skipped = 0
    errors = []
    stocks = _fetch_user_stocks(session["user_id"])

    # Batch-fetch quotes for all symbols at once where the provider supports it.
    symbols = [s.get("Symbol", "").strip() for s in stocks if s.get("Symbol")]
    quotes = market_data.get_quotes(symbols) if symbols else {}

    for s in stocks:
        symbol = (s.get("Symbol") or "").strip()
        if not symbol:
            skipped += 1
            continue
        try:
            quote = quotes.get(symbol) or market_data.get_quote(symbol) or {}
            price = quote.get("price")
            prev_close = quote.get("prev_close")
            if not price:
                skipped += 1
                continue
            pk = s.get("PartitionKey", "stock")
            try:
                entity = stocks_table_client.get_entity(
                    partition_key=pk, row_key=s["RowKey"])
                entity["CurrentPrice"] = float(price)
                if prev_close:
                    entity["PreviousClose"] = float(prev_close)
                stocks_table_client.update_entity(entity=entity, mode=UpdateMode.MERGE)
                updated += 1
            except ResourceNotFoundError:
                skipped += 1
        except Exception as e:
            errors.append(f"{symbol}: {e}")
    if updated:
        precompute.invalidate_user(session["user_id"])
    return jsonify({"updated": updated, "skipped": skipped, "errors": errors})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/provider")
def provider_info():
    """Return the active market-data provider name."""
    from application.services.market_data import provider_name
    return jsonify({"provider": provider_name()})


@app.route("/tv-chart")
def tv_chart():
    """Standalone page that renders a candlestick chart using TradingView's
    open-source Lightweight Charts library, fed by our /api/tv-ohlc endpoint.

    We render the chart ourselves (instead of using TradingView's hosted
    /widgetembed or Advanced Chart Widget) because both of those free embeds
    enforce a licensing whitelist that blocks most NSE symbols (SIEMENS,
    COALINDIA, TATASTEEL, …) with a "This symbol is only available on
    TradingView" notice.
    """
    raw_sym = (request.args.get("symbol") or "RELIANCE").strip().upper()
    # Drop any exchange prefix the embed code passes in (e.g. "NSE:RELIANCE")
    if ":" in raw_sym:
        raw_sym = raw_sym.split(":", 1)[1]
    sym = "".join(c for c in raw_sym if c.isalnum() or c in "._-") or "RELIANCE"
    tf = (request.args.get("interval") or "D").strip()
    tf = "".join(c for c in tf if c.isalnum())[:4] or "D"

    html = """<!doctype html><html><head><meta charset="utf-8">
<title>__SYM__ · Chart</title>
<style>
  html,body{height:100%;width:100%;margin:0;padding:0;background:#f8fafc;
    font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1f2937}
  .bar{display:flex;align-items:center;gap:6px;padding:6px 10px;
    border-bottom:1px solid #e5e7eb;background:#fff}
  .bar .sym{font-weight:700;font-size:14px;margin-right:8px}
  .bar button{border:1px solid #d1d5db;background:#fff;color:#374151;
    padding:3px 8px;border-radius:6px;font-size:12px;cursor:pointer}
  .bar button.on{background:#4f46e5;color:#fff;border-color:#4f46e5}
  .bar .sep{width:1px;height:18px;background:#e5e7eb;margin:0 4px}
  .bar button.tg{border-color:#cbd5e1}
  .bar button.tg.on{background:#0ea5e9;border-color:#0ea5e9}
  .bar .status{margin-left:auto;font-size:12px;color:#6b7280}
  #chart{position:absolute;left:0;right:0;bottom:0;top:38px}
  .ohlc{font-size:12px;color:#6b7280;margin-left:10px}
  .ohlc span{margin-right:8px}
  .ohlc .up{color:#059669}.ohlc .dn{color:#dc2626}
</style></head><body>
<div class="bar">
  <span class="sym">__SYM__</span>
  <button data-tf="5">5m</button>
  <button data-tf="15">15m</button>
  <button data-tf="60">1h</button>
  <button data-tf="D">1D</button>
  <button data-tf="W">1W</button>
  <span class="sep"></span>
  <button id="btnVol" class="tg on" title="Toggle volume">Volume</button>
  <button id="btnEma" class="tg on" title="Toggle 20 EMA">EMA 20</button>
  <span class="ohlc" id="ohlc"></span>
  <span class="status" id="status">Loading…</span>
</div>
<div id="chart"></div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
(function(){
  const SYM = "__SYM__";
  let curTf = "__TF__";
  const chart = LightweightCharts.createChart(document.getElementById('chart'), {
    layout:{background:{color:'#f8fafc'}, textColor:'#1f2937'},
    grid:{vertLines:{color:'#e5e7eb'}, horzLines:{color:'#e5e7eb'}},
    rightPriceScale:{borderColor:'#d1d5db'},
    timeScale:{borderColor:'#d1d5db', timeVisible:true, secondsVisible:false},
    crosshair:{mode:1}
  });
  const series = chart.addCandlestickSeries({
    upColor:'#059669', downColor:'#dc2626',
    borderUpColor:'#059669', borderDownColor:'#dc2626',
    wickUpColor:'#059669', wickDownColor:'#dc2626'
  });
  const volSeries = chart.addHistogramSeries({
    priceFormat:{type:'volume'}, priceScaleId:'', color:'#94a3b8',
    scaleMargins:{top:0.8, bottom:0}
  });
  const emaSeries = chart.addLineSeries({
    color:'#f59e0b', lineWidth:2, priceLineVisible:false, lastValueVisible:false,
    crosshairMarkerVisible:false
  });
  function computeEma(bars, period){
    const k = 2/(period+1); let prev=null; const out=[];
    for(let i=0;i<bars.length;i++){
      const c = bars[i].c;
      prev = (prev===null) ? c : (c*k + prev*(1-k));
      out.push({time:bars[i].t, value:prev});
    }
    return out;
  }
  let showVol=true, showEma=true;
  const ohlcEl = document.getElementById('ohlc');
  const statusEl = document.getElementById('status');
  chart.subscribeCrosshairMove(p=>{
    if(!p || !p.time || !p.seriesData) { ohlcEl.textContent=''; return; }
    const d = p.seriesData.get(series);
    if(!d){ ohlcEl.textContent=''; return; }
    const cls = d.close>=d.open?'up':'dn';
    ohlcEl.innerHTML = '<span>O '+d.open.toFixed(2)+'</span>'
      +'<span>H '+d.high.toFixed(2)+'</span>'
      +'<span>L '+d.low.toFixed(2)+'</span>'
      +'<span class="'+cls+'">C '+d.close.toFixed(2)+'</span>';
  });
  window.addEventListener('resize', ()=>chart.timeScale().fitContent());

  function load(tf){
    curTf = tf;
    document.querySelectorAll('.bar button[data-tf]').forEach(b=>{
      b.classList.toggle('on', b.dataset.tf===tf);
    });
    statusEl.textContent = 'Loading…';
    fetch('/api/tv-ohlc?symbol='+encodeURIComponent(SYM)+'&interval='+encodeURIComponent(tf))
      .then(r=>r.json()).then(j=>{
        if(!j.ok){ statusEl.textContent = j.error || 'No data'; return; }
        const bars = j.bars || [];
        if(!bars.length){ statusEl.textContent='No data'; series.setData([]); volSeries.setData([]); emaSeries.setData([]); return; }
        series.setData(bars.map(b=>({time:b.t, open:b.o, high:b.h, low:b.l, close:b.c})));
        volSeries.setData(bars.map(b=>({time:b.t, value:b.v||0,
          color: b.c>=b.o ? 'rgba(5,150,105,0.4)' : 'rgba(220,38,38,0.4)'})));
        emaSeries.setData(computeEma(bars, 20));
        chart.timeScale().fitContent();
        statusEl.textContent = bars.length + ' bars · ' + (j.source || '');
      })
      .catch(e=>{ statusEl.textContent = 'Error: '+e.message; });
  }
  document.querySelectorAll('.bar button[data-tf]').forEach(b=>{
    b.addEventListener('click', ()=>load(b.dataset.tf));
  });
  document.getElementById('btnVol').addEventListener('click', function(){
    showVol=!showVol; this.classList.toggle('on', showVol);
    volSeries.applyOptions({visible:showVol});
  });
  document.getElementById('btnEma').addEventListener('click', function(){
    showEma=!showEma; this.classList.toggle('on', showEma);
    emaSeries.applyOptions({visible:showEma});
  });
  load(curTf);
})();
</script></body></html>"""
    html = html.replace("__SYM__", sym).replace("__TF__", tf)
    from flask import Response
    return Response(html, mimetype="text/html")


@app.route("/api/tv-ohlc")
def tv_ohlc():
    """OHLC bars feed for the in-app chart drawer.

    Accepts ?symbol=RELIANCE&interval=D (TradingView-style interval codes:
    5, 15, 60, D, W) and returns:
      {ok:true, source:'yfinance', bars:[{t,o,h,l,c,v}, ...]}
    Times are UNIX seconds (UTC) — Lightweight Charts treats them as UTC
    business times when secondsVisible=false.
    """
    from application.services import market_data
    import pandas as pd

    raw_sym = (request.args.get("symbol") or "").strip().upper()
    if ":" in raw_sym:
        raw_sym = raw_sym.split(":", 1)[1]
    sym = "".join(c for c in raw_sym if c.isalnum() or c in "._-")
    if not sym:
        return jsonify({"ok": False, "error": "missing symbol"}), 400

    tf = (request.args.get("interval") or "D").strip().upper()
    # TV code → (yfinance interval, lookback days)
    tf_map = {
        "5":  ("5m",  7),
        "15": ("15m", 30),
        "60": ("60m", 60),
        "D":  ("1d",  365 * 2),
        "W":  ("1wk", 365 * 5),
    }
    yf_interval, days = tf_map.get(tf, ("1d", 365 * 2))

    # market_data providers expect Yahoo-style suffix (.NS) for NSE.
    yahoo_sym = sym if "." in sym else f"{sym}.NS"

    try:
        df = market_data.get_history(yahoo_sym, days=days, interval=yf_interval)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"fetch failed: {exc}"}), 502

    if df is None or getattr(df, "empty", True):
        return jsonify({"ok": False, "error": "no data for symbol"}), 404

    # Normalise column names (different providers may return lower/upper).
    cols = {c.lower(): c for c in df.columns}
    o_col = cols.get("open"); h_col = cols.get("high")
    l_col = cols.get("low");  c_col = cols.get("close")
    v_col = cols.get("volume")
    if not (o_col and h_col and l_col and c_col):
        return jsonify({"ok": False, "error": "unexpected data shape"}), 500

    bars = []
    for ts, row in df.iterrows():
        try:
            # Lightweight Charts always renders UNIX times in UTC. Feed it the
            # IST wall-clock reinterpreted as UTC so the axis/crosshair show IST.
            ts2 = pd.Timestamp(ts)
            if ts2.tzinfo is not None:
                ts2 = ts2.tz_convert("Asia/Kolkata").tz_localize(None)
            t_val = int(ts2.tz_localize("UTC").timestamp())
            bars.append({
                "t": t_val,
                "o": float(row[o_col]),
                "h": float(row[h_col]),
                "l": float(row[l_col]),
                "c": float(row[c_col]),
                "v": float(row[v_col]) if v_col and pd.notna(row[v_col]) else 0.0,
            })
        except Exception:
            continue

    # Lightweight Charts rejects a series whose points are not strictly
    # ascending and unique by time — when that happens the axes still render
    # but no candles draw. Providers (or a fyers+yfinance merge) can return
    # overlapping or out-of-order bars, so normalise here: sort ascending and
    # keep the last bar for any duplicated timestamp.
    if bars:
        bars.sort(key=lambda b: b["t"])
        deduped: dict = {}
        for b in bars:
            deduped[b["t"]] = b
        bars = list(deduped.values())

    return jsonify({
        "ok": True,
        "symbol": sym,
        "interval": tf,
        "source": market_data.provider_name(),
        "bars": bars,
    })

