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


# ---------- Helpers ----------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "email" not in session or "user_id" not in session:
            return redirect(url_for("logIn"))
        return view(*args, **kwargs)
    return wrapped


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
    users = list(user_table_client.query_entities(
        query_filter=f"Email eq '{email}'"))
    return users[0] if users else None


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
    return redirect(url_for("logIn"))


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
            }
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


# ---------- Pages ----------

@app.route("/home")
@login_required
def home():
    stocks = _fetch_user_stocks(session["user_id"])
    just_logged_in = session.pop("just_logged_in", False)
    return render_template("home.html",
                           name=session["name"], email=session["email"],
                           title="Dashboard", stocks=stocks,
                           just_logged_in=just_logged_in)


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
        return jsonify({"ok": True})
    except ResourceNotFoundError:
        # Try with old PartitionKey value used by legacy data
        try:
            stocks_table_client.delete_entity(
                partition_key="PartitionKey", row_key=row_key)
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
    return jsonify({"updated": updated, "skipped": skipped, "errors": errors})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/provider")
def provider_info():
    """Return the active market-data provider name."""
    from application.services.market_data import provider_name
    return jsonify({"provider": provider_name()})

