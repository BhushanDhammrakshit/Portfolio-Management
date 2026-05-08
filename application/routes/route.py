"""Core HTML routes for Portfolio Manager."""
import uuid
from functools import wraps

from flask import (render_template, request, redirect, session, url_for,
                   jsonify, flash)
from werkzeug.security import generate_password_hash, check_password_hash
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

from application import app
from application.services.azure_table import (user_table_client,
                                              stocks_table_client)


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
            }
            user_table_client.create_entity(entity=entity)
            session["name"] = name
            session["email"] = email
            session["user_id"] = entity["RowKey"]
            return redirect(url_for("home"))
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


# ---------- Pages ----------

@app.route("/home")
@login_required
def home():
    stocks = _fetch_user_stocks(session["user_id"])
    just_logged_in = session.pop("just_logged_in", False)
    return render_template("home.html",
                           name=session["name"], email=session["email"],
                           title="Home", stocks=stocks,
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
                           title="Algo Helper", stocks=stocks)


@app.route("/advanced-dashboard")
@login_required
def advanced_dashboard():
    return render_template("advancedDashboard.html",
                           name=session["name"], email=session["email"],
                           title="Advanced Analytics")


@app.route("/portfolioMaker")
@login_required
def portfolioMaker():
    stocks = _fetch_user_stocks(session["user_id"])
    return render_template("portfolioMaker.html",
                           name=session["name"], email=session["email"],
                           stocks=stocks,
                           title="Portfolio Maker")


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
                           title="Profile")


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

        # Auto-fetch current price (and fill sector when missing) from yfinance
        current_price = 0.0
        if symbol:
            try:
                import yfinance as yf
                ticker = yf.Ticker(symbol)
                info = {}
                try:
                    info = ticker.info or {}
                except Exception:
                    info = {}
                fast = getattr(ticker, "fast_info", None)
                price = None
                if fast:
                    for k in ("last_price", "lastPrice", "regular_market_price"):
                        v = fast.get(k) if hasattr(fast, "get") else None
                        if v:
                            price = float(v)
                            break
                if price is None:
                    hist = ticker.history(period="1d")
                    if not hist.empty:
                        price = float(hist["Close"].iloc[-1])
                if price is not None:
                    current_price = price
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
    """Use yfinance to refresh CurrentPrice for any stock with a Symbol set."""
    import yfinance as yf
    updated = 0
    skipped = 0
    errors = []
    stocks = _fetch_user_stocks(session["user_id"])
    for s in stocks:
        symbol = (s.get("Symbol") or "").strip()
        if not symbol:
            skipped += 1
            continue
        try:
            ticker = yf.Ticker(symbol)
            price = None
            try:
                fi = ticker.fast_info
                price = fi["last_price"]
            except Exception:
                pass
            if not price:
                hist = ticker.history(period="1d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
            if not price:
                skipped += 1
                continue
            # Get previous close for day's gain tracking
            prev_close = None
            try:
                fi = ticker.fast_info
                prev_close = getattr(fi, 'previous_close', None) or fi.get('previousClose', None) if hasattr(fi, 'get') else getattr(fi, 'previous_close', None)
            except Exception:
                pass
            if not prev_close:
                try:
                    info = ticker.info or {}
                    prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
                except Exception:
                    pass
            if not prev_close:
                try:
                    hist2 = ticker.history(period="5d")
                    if len(hist2) >= 2:
                        prev_close = float(hist2["Close"].iloc[-2])
                except Exception:
                    pass
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

