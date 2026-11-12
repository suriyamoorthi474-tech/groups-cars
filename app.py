import os
import re
import time
import hmac
from pathlib import Path
from datetime import datetime, timedelta, timezone
import random

from flask import (
    Flask, render_template, request,
    jsonify, session, redirect, url_for
)
from dotenv import load_dotenv
from supabase import create_client


# --------------------------------
# 1. LOAD ENVIRONMENT VARIABLES
# --------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_FILE, override=True)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase credentials missing. Check your .env file.")

if not FLASK_SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY missing in .env file.")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError("Admin credentials missing in .env file.")


# --------------------------------
# 2. FLASK CONFIGURATION
# --------------------------------

app = Flask(__name__)

app.secret_key = FLASK_SECRET_KEY

# Stay logged in until the user clicks Logout.
# The cookie is renewed on every request, so an active user is never logged out.
# Effectively never expires on its own (10 years). Only Logout ends it.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=3650)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Local development only.
# Set this to True when deploying with HTTPS:
app.config["SESSION_COOKIE_SECURE"] = False


# Database client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


@app.template_filter("inr")
def format_inr(value):
    """1400000 -> 14,00,000 (Indian digit grouping). Use as {{ price|inr }}."""

    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return ""

    digits = str(abs(number))

    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts) + "," + tail

    return ("-" if number < 0 else "") + digits

# Indian Standard Time (used for dates in leads and ads)
IST = timezone(timedelta(hours=5, minutes=30))

# Steps of a confirmed deal, in order
DEAL_STATUSES = ["Confirmed", "Contacted", "Booked", "Delivered"]

# ---- Vehicle types ----------------------------------------------
# Cars, bikes and scooters all live in the same "cars" table and use the
# same groups / offers / votes / deals. To add a new vehicle type later
# (Auto, E-Rickshaw, ...), just add one more name to this list.
VEHICLE_TYPES = ["Car", "Bike", "Scooter", "Electric Scooter"]
DEFAULT_VEHICLE_TYPE = "Car"


def clean_vehicle_type(value):
    """Returns a valid vehicle type, or None if the value is not allowed.
    An empty value becomes the default ("Car")."""

    value = str(value or DEFAULT_VEHICLE_TYPE).strip()
    return value if value in VEHICLE_TYPES else None


# ---- Cities (used for location-based ads) -----------------------
# The customer picks one of these cities, and an ad can target one of them.
# An ad with no city shows to everyone. Add more cities here any time.
CITIES = [
    "Chennai", "Coimbatore", "Madurai", "Tiruchirappalli", "Salem",
    "Tirunelveli", "Erode", "Vellore", "Tiruppur", "Thanjavur",
    "Puducherry", "Bengaluru", "Hyderabad", "Kochi", "Thiruvananthapuram",
    "Mumbai", "Pune", "Delhi", "Kolkata", "Ahmedabad",
]


def clean_city(value):
    """Customers and admin can also TYPE a city that is not in CITIES.
    Returns the cleaned city, '' for empty, or None if it looks invalid.
    A city from CITIES always keeps its standard spelling, so a typed
    'chennai ' matches ads set to 'Chennai'."""

    value = re.sub(r"\s+", " ", str(value or "")).strip()

    if not value:
        return ""

    if len(value) > 60 or not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", value):
        return None

    for city in CITIES:
        if city.lower() == value.lower():
            return city

    return value.title()


# Used to hide phone numbers, emails and links in offer notes
CONTACT_PATTERN = re.compile(
    r"(\+?\d[\d\s\-]{7,}\d)"           # phone numbers
    r"|([\w\.-]+@[\w\.-]+\.\w+)"       # emails
    r"|(https?://\S+|www\.\S+)",       # links
    re.IGNORECASE
)


# Create a separate Auth client for each authentication operation
def get_auth_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# --------------------------------
# 3. SESSION + SHARED HELPERS
# --------------------------------

def is_admin_logged_in():
    return (
        session.get("is_admin") is True
        and session.get("admin_id") == "env-admin"
    )


def admin_denied():
    return jsonify({
        "success": False,
        "message": "Admin access denied."
    }), 403


def execute_with_retry(build_query, attempts=3):
    """
    Runs a Supabase query and retries a few times if the connection
    was dropped (common after the app has been idle for a few minutes).
    """

    last_error = None

    for attempt in range(attempts):
        try:
            return build_query().execute()
        except Exception as error:
            last_error = error
            print(f"Supabase retry {attempt + 1}/{attempts}:", str(error))
            time.sleep(0.4)

    raise last_error


# Each role keeps its own keys in the session cookie, so logging in or
# out as one role never logs out another role in the same browser.
CUSTOMER_SESSION_KEYS = (
    "user_id", "email",
    "cached_role", "cached_name", "cached_phone", "cached_city",
)
ADMIN_SESSION_KEYS = ("admin_id", "admin_email", "is_admin")


def clear_customer_session():
    for key in CUSTOMER_SESSION_KEYS:
        session.pop(key, None)


def clear_admin_session():
    for key in ADMIN_SESSION_KEYS:
        session.pop(key, None)


# ---- Seat rules -------------------------------------------------
# A member row has a status:
#   pending    seat is held for HOLD_HOURS, waiting for admin verification
#   confirmed  verified serious buyer (counts for ever)
#   rejected / expired   not a member any more, the seat is free
# Set REQUIRE_VERIFICATION = False to confirm everyone instantly (old behaviour).
# When online payment is added, a paid member simply becomes "confirmed".
REQUIRE_VERIFICATION = True
HOLD_HOURS = 24


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def is_live_member(row):
    """True if this member row currently holds a seat."""

    status = row.get("status") or "confirmed"

    if status == "confirmed":
        return True

    if status == "pending":
        expires = parse_time(row.get("hold_expires_at"))
        return expires is None or expires > datetime.now(timezone.utc)

    return False


def display_status(row):
    """confirmed / pending / expired / rejected (a lapsed hold shows expired)."""

    status = row.get("status") or "confirmed"

    if status == "pending" and not is_live_member(row):
        return "expired"

    return status


def live_member_rows(group_ids):
    """Seat-holding member rows (confirmed + unexpired pending) for groups."""

    if not group_ids:
        return []

    rows = (
        supabase.table("car_group_members")
        .select("group_id, customer_id, status, hold_expires_at")
        .in_("group_id", list(group_ids))
        .execute()
    ).data or []

    return [row for row in rows if is_live_member(row)]


def group_member_count(group_id):
    """Seats taken = confirmed members + pending members whose hold is valid."""

    return len(live_member_rows([group_id]))


def group_confirmed_count(group_id):
    return sum(
        1 for row in live_member_rows([group_id])
        if (row.get("status") or "confirmed") == "confirmed"
    )


def member_status(group_id, customer_id):
    """'pending', 'confirmed' or None (not a live member)."""

    rows = (
        supabase.table("car_group_members")
        .select("status, hold_expires_at")
        .eq("group_id", group_id)
        .eq("customer_id", customer_id)
        .execute()
    ).data or []

    if rows and is_live_member(rows[0]):
        return rows[0].get("status") or "confirmed"

    return None


def customer_in_group(group_id, customer_id, confirmed_only=False):
    status = member_status(group_id, customer_id)

    if confirmed_only:
        return status == "confirmed"

    return status is not None


def strip_contact_info(text):
    return CONTACT_PATTERN.sub("[hidden]", text or "")


def saving_amount(showroom, price):
    """Showroom price - deal price, or None when either is missing
    or the deal is not cheaper."""

    try:
        showroom, price = float(showroom or 0), float(price or 0)
    except (TypeError, ValueError):
        return None

    return showroom - price if showroom and price and showroom > price else None


# --------------------------------
# 4. CUSTOMER PROFILE HELPER
# --------------------------------

def get_or_create_customer_profile(user):

    result = (
        supabase.table("profiles")
        .select("*")
        .eq("id", user.id)
        .limit(1)
        .execute()
    )

    profiles = result.data or []

    if profiles:
        return profiles[0]

    metadata = user.user_metadata or {}

    profile_data = {
        "id": user.id,
        "full_name": metadata.get("full_name") or "",
        "phone": metadata.get("phone"),
        "role": "customer"
    }

    created = (
        supabase.table("profiles")
        .insert(profile_data)
        .execute()
    )

    return created.data[0] if created.data else profile_data


# --------------------------------
# 5. VERIFY CUSTOMER SESSION
# --------------------------------

def get_logged_in_customer():
    """
    The session cookie is signed with FLASK_SECRET_KEY, so the user id inside
    it can be trusted. We do not ask Supabase to re-check an access token on
    every request (those tokens expire after about an hour and caused random
    logouts). The customer profile is loaded from the database instead.
    """

    user_id = session.get("user_id")

    if not user_id:
        return None

    profile = None
    loaded = False

    for attempt in range(3):
        try:
            result = (
                supabase.table("profiles")
                .select("*")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )

            profile = result.data[0] if result.data else None
            loaded = True
            break

        except Exception as error:
            print("Customer profile error:", str(error))
            time.sleep(0.4)

    # Database temporarily unreachable: stay logged in using the details
    # saved in the session at login time, instead of throwing the user out.
    if not loaded:
        if session.get("cached_role") == "customer":
            return {
                "id": user_id,
                "email": session.get("email", ""),
                "full_name": session.get("cached_name", ""),
                "phone": session.get("cached_phone", ""),
                "city": session.get("cached_city", "")
            }
        return None

    # The account no longer exists
    if not profile:
        clear_customer_session()
        return None

    if profile.get("role") != "customer":
        return None

    session["cached_role"] = "customer"
    session["cached_name"] = profile.get("full_name", "") or ""
    session["cached_phone"] = profile.get("phone", "") or ""
    session["cached_city"] = profile.get("city", "") or ""

    return {
        "id": user_id,
        "email": session.get("email", ""),
        "full_name": profile.get("full_name", ""),
        "phone": profile.get("phone", ""),
        "city": profile.get("city", "") or ""
    }


# --------------------------------
# 6. HOME PAGE
# --------------------------------

@app.route("/")
def home():

    # Approved customer reviews for the landing page.
    # If the reviews table is missing or the query fails, the page still opens.
    try:
        testimonials, review_summary = load_public_testimonials()
    except Exception as error:
        print("Testimonials error:", str(error))
        testimonials, review_summary = [], None

    return render_template(
        "index.html",
        testimonials=testimonials,
        review_summary=review_summary
    )


# --------------------------------
# 7. CUSTOMER REGISTRATION PAGE
# --------------------------------

@app.route("/register")
def register():
    return render_template("register.html")


# --------------------------------
# 8. CUSTOMER REGISTRATION API
# --------------------------------

@app.route("/api/register", methods=["POST"])
def api_register():

    data = request.get_json(silent=True) or {}

    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    phone = data.get("phone", "").strip()
    password = data.get("password", "")

    if not name or not email or not phone or not password:
        return jsonify({
            "success": False,
            "message": "Please fill all fields."
        }), 400

    if len(password) < 8:
        return jsonify({
            "success": False,
            "message": "Password must contain at least 8 characters."
        }), 400

    try:
        auth = get_auth_client()

        auth.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {
                    "full_name": name,
                    "phone": phone
                }
            }
        })

        return jsonify({
            "success": True,
            "message": "Registration successful! Check your email if confirmation is required."
        })

    except Exception as error:
        print("Registration error:", str(error))

        return jsonify({
            "success": False,
            "message": "Registration failed. Check your details or try another email."
        }), 400


# --------------------------------
# 9. CUSTOMER LOGIN PAGE
# --------------------------------

@app.route("/login")
def login():

    if session.get("user_id"):
        customer = get_logged_in_customer()

        if customer:
            return redirect(url_for("customer_dashboard"))

    return render_template("login.html")


# --------------------------------
# 10. CUSTOMER LOGIN API
# --------------------------------

@app.route("/api/login", methods=["POST"])
def api_login():

    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({
            "success": False,
            "message": "Enter email and password."
        }), 400

    try:
        auth = get_auth_client()

        result = auth.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        if not result.session or not result.user:
            return jsonify({
                "success": False,
                "message": "Login failed."
            }), 401

        # Make sure the customer profile exists
        get_or_create_customer_profile(result.user)

        # Start a fresh customer session (admin session is untouched)
        clear_customer_session()

        session["user_id"] = result.user.id
        session["email"] = result.user.email
        session.permanent = True

        # Verify customer profile and role
        customer = get_logged_in_customer()

        if not customer:
            clear_customer_session()

            return jsonify({
                "success": False,
                "message": "Customer profile unavailable or account access not permitted."
            }), 403

        return jsonify({
            "success": True,
            "message": "Login successful!"
        })

    except Exception as error:
        print("Login error:", str(error))

        return jsonify({
            "success": False,
            "message": "Invalid email or password. Check your details and try again."
        }), 401


# --------------------------------
# 11. CUSTOMER PAGES (each page is separate)
# --------------------------------

def load_all_cars(vehicle_type=None):
    """All vehicles, newest first. Pass a vehicle type (Car / Bike / ...)
    to show only that type."""

    try:
        query = (
            supabase.table("cars")
            .select("*")
            .order("created_at", desc=True)
        )

        if vehicle_type in VEHICLE_TYPES:
            query = query.eq("vehicle_type", vehicle_type)

        return query.execute().data or []
    except Exception as error:
        print("Customer cars loading error:", str(error))
        return []


# Every customer page is separate. Each one checks the login first.

@app.route("/customer-dashboard")
def customer_dashboard():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    return render_template(
        "customer/home.html",
        customer=customer,
        active_page="home",
        car_count=len(load_all_cars())
    )


@app.route("/customer/cars")
def customer_cars_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    # Optional filter: /customer/cars?type=Bike
    selected_type = request.args.get("type", "").strip()

    if selected_type not in VEHICLE_TYPES:
        selected_type = ""

    # Cars this customer already showed interest in (so the page can
    # remember them after a refresh / next login)
    try:
        rows = (
            supabase.table("car_interests")
            .select("car_id")
            .eq("customer_id", customer["id"])
            .execute()
        ).data or []
        interested_ids = [row["car_id"] for row in rows]
    except Exception as error:
        print("Load interests error:", str(error))
        interested_ids = []

    return render_template(
        "customer/cars.html",
        customer=customer,
        active_page="cars",
        cars=load_all_cars(selected_type or None),
        interested_ids=interested_ids,
        vehicle_types=VEHICLE_TYPES,
        selected_type=selected_type
    )


@app.route("/customer/available-groups")
def customer_available_groups_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    # Groups are loaded here and sent with the page, so the cards show
    # instantly instead of waiting for a second request from the browser.
    try:
        initial_groups = build_customer_groups(customer["id"])
    except Exception as error:
        print("Available groups page error:", str(error))
        initial_groups = None

    return render_template(
        "customer/available_groups.html",
        customer=customer,
        active_page="available",
        initial_groups=initial_groups,
        vehicle_types=VEHICLE_TYPES
    )


@app.route("/customer/my-groups")
def customer_my_groups_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    return render_template(
        "customer/my_groups.html",
        customer=customer,
        active_page="mygroups"
    )


@app.route("/customer/groups/<int:group_id>/offers")
def customer_group_offers_page(group_id):

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    try:
        # Only members of the group can open its offers page
        if not customer_in_group(group_id, customer["id"]):
            return redirect(url_for("customer_my_groups_page"))

        found = (
            supabase.table("car_groups")
            .select("id, group_name")
            .eq("id", group_id)
            .execute()
        ).data

    except Exception as error:
        print("Offers page error:", str(error))
        return redirect(url_for("customer_my_groups_page"))

    if not found:
        return redirect(url_for("customer_my_groups_page"))

    return render_template(
        "customer/group_offers.html",
        customer=customer,
        active_page="mygroups",
        group=found[0]
    )


@app.route("/customer/emi")
def customer_emi_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    return render_template(
        "customer/emi.html",
        customer=customer,
        active_page="emi",
        vehicle_types=VEHICLE_TYPES
    )


def build_customer_purchases(customer_id):
    """
    Every deal the customer is part of that has been finalized
    (group Closed or Completed): vehicle, group, final price, delivery
    estimate and deal progress. The dealer's name is never included.
    Active deals come first, delivered ones after (newest first).
    """

    member_rows = (
        supabase.table("car_group_members")
        .select("group_id, status, hold_expires_at")
        .eq("customer_id", customer_id)
        .execute()
    ).data or []

    group_ids = [row["group_id"] for row in member_rows if is_live_member(row)]

    if not group_ids:
        return []

    groups = (
        supabase.table("car_groups")
        .select("*")
        .in_("id", group_ids)
        .in_("status", ["Closed", "Completed"])
        .execute()
    ).data or []

    if not groups:
        return []

    ids = [group["id"] for group in groups]
    car_ids = list({group["car_id"] for group in groups if group.get("car_id")})

    cars = {}
    if car_ids:
        cars = {
            row["id"]: row
            for row in (
                supabase.table("cars")
                .select("id, brand, model, variant, image_url, vehicle_type, showroom_price")
                .in_("id", car_ids)
                .execute()
            ).data or []
        }

    deals = {
        row["group_id"]: row
        for row in (
            supabase.table("dealer_offers")
            .select("group_id, price, delivery_days, deal_status, deal_updated_at")
            .in_("group_id", ids)
            .eq("status", "Accepted")
            .execute()
        ).data or []
    }

    # This customer's own reviews (missing table must not break the profile)
    my_reviews = {}
    try:
        my_reviews = {
            row["group_id"]: row
            for row in (
                supabase.table("purchase_ratings")
                .select("group_id, stars, comment, public_consent, status")
                .eq("customer_id", customer_id)
                .in_("group_id", ids)
                .execute()
            ).data or []
        }
    except Exception as review_error:
        print("My reviews error:", str(review_error))

    purchases = []

    for group in groups:
        deal = deals.get(group["id"]) or {}
        car = cars.get(group.get("car_id")) or {}

        deal_status = deal.get("deal_status") or "Confirmed"
        delivered = (
            group["status"] == "Completed" or deal_status == "Delivered"
        )

        purchases.append({
            "review": my_reviews.get(group["id"]),
            "saving": saving_amount(car.get("showroom_price"), deal.get("price")),
            "group_id": group["id"],
            "group_name": group.get("group_name"),
            "car": car,
            "price": deal.get("price"),
            "delivery_days": deal.get("delivery_days"),
            "deal_status": "Delivered" if delivered else deal_status,
            "step": (
                DEAL_STATUSES.index("Delivered") if delivered
                else (DEAL_STATUSES.index(deal_status)
                      if deal_status in DEAL_STATUSES else 0)
            ),
            "delivered": delivered,
            "updated_at": deal.get("deal_updated_at"),
            "completed_at": group.get("completed_at")
        })

    purchases.sort(
        key=lambda p: (
            p["delivered"],
            -(parse_time(p["updated_at"] or p["completed_at"]) or
              datetime.min.replace(tzinfo=timezone.utc)).timestamp()
        )
    )

    return purchases


@app.route("/customer/profile")
def customer_profile_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    try:
        purchases = build_customer_purchases(customer["id"])
    except Exception as error:
        print("Profile purchases error:", str(error))
        purchases = []

    return render_template(
        "customer/profile.html",
        customer=customer,
        active_page="profile",
        purchases=purchases,
        deal_steps=DEAL_STATUSES
    )


@app.route("/api/customer/city", methods=["POST"])
def customer_set_city():
    """Saves the city the customer picked (empty = clear). Used for local ads."""

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    data = request.get_json(silent=True) or {}
    city = clean_city(data.get("city"))

    if city is None:
        return jsonify({
            "success": False,
            "message": "Select a valid city."
        }), 400

    try:
        supabase.table("profiles").update(
            {"city": city or None}
        ).eq("id", customer["id"]).execute()

        session["cached_city"] = city

        return jsonify({
            "success": True,
            "city": city,
            "message": "City saved: " + city if city else "City cleared."
        })

    except Exception as error:
        print("Set city error:", str(error))

        hint = (
            " Run the profiles.city SQL in Supabase first."
            if "city" in str(error).lower() else ""
        )

        return jsonify({
            "success": False,
            "message": "Unable to save your city." + hint
        }), 500


@app.route("/api/car-interest", methods=["POST"])
def add_car_interest():

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    data = request.get_json(silent=True) or {}
    car_id = data.get("car_id")

    if not car_id:
        return jsonify({
            "success": False,
            "message": "Car ID is required."
        }), 400

    try:
        supabase.table("car_interests").insert({
            "customer_id": customer["id"],
            "car_id": int(car_id)
        }).execute()

        return jsonify({
            "success": True,
            "message": "Your interest has been submitted successfully!"
        })

    except Exception as error:
        error_message = str(error)

        if "23505" in error_message or "duplicate key" in error_message.lower():
            return jsonify({
                "success": True,
                "message": "You have already shown interest in this car."
            })

        print("Car interest error:", error_message)

        return jsonify({
            "success": False,
            "message": "Unable to submit interest. Please try again."
        }), 500


@app.route("/api/car-interest/remove", methods=["POST"])
def remove_car_interest():

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    data = request.get_json(silent=True) or {}

    try:
        car_id = int(data.get("car_id"))
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "message": "Car ID is required."
        }), 400

    try:
        supabase.table("car_interests").delete().eq(
            "customer_id", customer["id"]
        ).eq("car_id", car_id).execute()

        return jsonify({
            "success": True,
            "message": "Your interest has been removed."
        })

    except Exception as error:
        print("Remove interest error:", str(error))

        return jsonify({
            "success": False,
            "message": "Unable to remove interest. Please try again."
        }), 500


# --------------------------------
# CUSTOMER: VIEW GROUPS
# Open groups + any group the customer joined (even if Closed)
# --------------------------------

def build_customer_groups(customer_id):
    """
    Open groups + every group the customer joined, with car, seats, offer
    prices and deal progress. Uses a fixed number of queries (not one set of
    queries per group), so the page stays fast as groups grow.
    """

    joined_rows = [
        row for row in (
            supabase.table("car_group_members")
            .select("group_id, status, hold_expires_at")
            .eq("customer_id", customer_id)
            .execute()
        ).data or []
        if is_live_member(row)
    ]

    joined_ids = {row["group_id"] for row in joined_rows}
    my_status = {
        row["group_id"]: row.get("status") or "confirmed"
        for row in joined_rows
    }

    groups = (
        supabase.table("car_groups")
        .select("*")
        .eq("status", "Open")
        .order("created_at", desc=True)
        .execute()
    ).data or []

    open_ids = {group["id"] for group in groups}
    extra_ids = [gid for gid in joined_ids if gid not in open_ids]

    if extra_ids:
        groups.extend(
            (
                supabase.table("car_groups")
                .select("*")
                .in_("id", extra_ids)
                .execute()
            ).data or []
        )

    if not groups:
        return []

    group_ids = [group["id"] for group in groups]
    car_ids = list({group["car_id"] for group in groups})

    # 1 query: all cars / bikes (vehicle_type is included so the group
    # card can show a "Bike" / "Scooter" badge)
    cars = {
        row["id"]: row
        for row in (
            supabase.table("cars")
            .select("id, brand, model, variant, image_url, vehicle_type, showroom_price")
            .in_("id", car_ids)
            .execute()
        ).data or []
    }

    # 1 query: seat counts for all groups (confirmed + pending holds)
    member_counts = {}
    confirmed_counts = {}
    for row in live_member_rows(group_ids):
        gid = row["group_id"]
        member_counts[gid] = member_counts.get(gid, 0) + 1
        if (row.get("status") or "confirmed") == "confirmed":
            confirmed_counts[gid] = confirmed_counts.get(gid, 0) + 1

    # 1 query: live offers (dealer name is never sent to customers)
    price_map = {}
    deal_map = {}
    for row in (
        supabase.table("dealer_offers")
        .select("group_id, price, status, deal_status")
        .in_("group_id", group_ids)
        .in_("status", ["Active", "Selected", "Accepted"])
        .execute()
    ).data or []:
        price_map.setdefault(row["group_id"], []).append(float(row["price"]))

        if row["status"] == "Accepted":
            deal_map[row["group_id"]] = row

    for group in groups:
        gid = group["id"]
        member_count = member_counts.get(gid, 0)
        prices = price_map.get(gid, [])

        group["car"] = {
            key: value
            for key, value in cars.get(group["car_id"], {}).items()
            if key != "id"
        }

        # Old rows without a type are shown as cars
        group["car"].setdefault("vehicle_type", DEFAULT_VEHICLE_TYPE)
        group["vehicle_type"] = (
            group["car"].get("vehicle_type") or DEFAULT_VEHICLE_TYPE
        )

        group["member_count"] = member_count
        group["confirmed_count"] = confirmed_counts.get(gid, 0)
        group["is_joined"] = gid in joined_ids
        group["my_status"] = my_status.get(gid)  # pending / confirmed / None

        # Offer prices are visible BEFORE the group fills up
        group["offer_count"] = len(prices)
        group["best_price"] = min(prices) if prices else None
        group["highest_price"] = max(prices) if prices else None

        # Showroom price and the real saving of the best offer
        # (None when the admin has not set a price, or the offer is not lower)
        showroom = group["car"].get("showroom_price")

        group["showroom_price"] = showroom or None
        group["best_saving"] = saving_amount(showroom, group["best_price"])

        group["seats_left"] = max(
            0, int(group.get("target_members") or 0) - member_count
        )

        # Deal progress is shown only to members of a closed group
        group["deal_status"] = None
        group["deal_price"] = None

        if group["status"] in ("Closed", "Completed") and group["is_joined"]:
            deal = deal_map.get(gid)

            if deal:
                group["deal_status"] = deal.get("deal_status") or "Confirmed"
                group["deal_price"] = deal["price"]

    return groups


@app.route("/api/customer/groups", methods=["GET"])
def customer_groups_api():

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    try:
        return jsonify({
            "success": True,
            "groups": build_customer_groups(customer["id"])
        })

    except Exception as error:
        print("Customer groups error:", str(error))

        return jsonify({
            "success": False,
            "message": "Unable to load groups."
        }), 500


# --------------------------------
# CUSTOMER: JOIN GROUP
# --------------------------------

def find_next_batch(group_id):
    """Another Open group for the same car that still has a free seat."""

    try:
        base = (
            supabase.table("car_groups")
            .select("car_id")
            .eq("id", group_id)
            .execute()
        ).data

        if not base:
            return None

        others = (
            supabase.table("car_groups")
            .select("id, target_members")
            .eq("car_id", base[0]["car_id"])
            .eq("status", "Open")
            .neq("id", group_id)
            .order("created_at")
            .execute()
        ).data or []

        taken = {}
        for row in live_member_rows([g["id"] for g in others]):
            taken[row["group_id"]] = taken.get(row["group_id"], 0) + 1

        for other in others:
            if taken.get(other["id"], 0) < int(other.get("target_members") or 0):
                return other["id"]

    except Exception as error:
        print("Next batch error:", str(error))

    return None


@app.route("/api/customer/groups/<int:group_id>/join",
           methods=["POST"])
def join_customer_group(group_id):

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    try:
        # The seat check and the insert happen together inside the database
        # (see cartogether_seats.sql), with the group row locked. So even if
        # 15 people press Join at the same moment, only target_members win.
        outcome = (
            supabase.rpc("join_group", {
                "p_group_id": group_id,
                "p_customer_id": customer["id"],
                "p_hold_hours": HOLD_HOURS,
                "p_require_verification": REQUIRE_VERIFICATION
            }).execute()
        ).data

        if isinstance(outcome, list):
            outcome = outcome[0] if outcome else {}

        result = (outcome or {}).get("result")

        if result == "not_found":
            return jsonify({
                "success": False,
                "message": "Group not found."
            }), 404

        if result == "closed":
            return jsonify({
                "success": False,
                "message": "This group is not open."
            }), 400

        if result == "rejected":
            return jsonify({
                "success": False,
                "message": "You cannot join this group."
            }), 403

        if result == "full":
            return jsonify({
                "success": False,
                "full": True,
                "next_group_id": find_next_batch(group_id),
                "message": "This group just filled up."
            }), 409

        if result == "already":
            return jsonify({
                "success": True,
                "status": outcome.get("status"),
                "message": "You have already joined this group."
            })

        status = outcome.get("status") or "confirmed"

        return jsonify({
            "success": True,
            "status": status,
            "message": (
                "Seat reserved! We will verify and confirm it shortly."
                if status == "pending"
                else "Successfully joined the group!"
            )
        })

    except Exception as error:
        print("Join group error:", str(error))

        return jsonify({
            "success": False,
            "message": "Unable to join group. Please try again."
        }), 500


# --------------------------------
# CUSTOMER: VIEW OFFERS FOR A JOINED GROUP
# Dealer identity is never sent to customers.
# --------------------------------

@app.route("/api/customer/groups/<int:group_id>/offers", methods=["GET"])
def customer_group_offers_api(group_id):

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    try:
        if not customer_in_group(group_id, customer["id"]):
            return jsonify({
                "success": False,
                "message": "Join this group to see its offers."
            }), 403

        group = (
            supabase.table("car_groups")
            .select("status, target_members")
            .eq("id", group_id)
            .execute()
        ).data

        group_status = group[0]["status"] if group else "Open"
        target_members = int(group[0].get("target_members") or 0) if group else 0

        # Showroom price of the vehicle, used to show real savings.
        # Kept in its own try so a missing price never breaks the offers page.
        showroom_price = None
        try:
            car_id = (
                supabase.table("car_groups")
                .select("car_id")
                .eq("id", group_id)
                .execute()
            ).data
            if car_id:
                car_row = (
                    supabase.table("cars")
                    .select("showroom_price")
                    .eq("id", car_id[0]["car_id"])
                    .execute()
                ).data
                if car_row and car_row[0].get("showroom_price"):
                    showroom_price = float(car_row[0]["showroom_price"])
        except Exception as price_error:
            print("Showroom price error:", str(price_error))

        offers = (
            supabase.table("dealer_offers")
            .select("id, price, delivery_days, notes, status, deal_status")
            .eq("group_id", group_id)
            .in_("status", ["Active", "Selected", "Accepted"])
            .order("price")
            .execute()
        ).data or []

        votes = (
            supabase.table("offer_votes")
            .select("offer_id, customer_id")
            .eq("group_id", group_id)
            .execute()
        ).data or []

        vote_counts = {}
        my_vote = None

        for vote in votes:
            offer_id = vote["offer_id"]
            vote_counts[offer_id] = vote_counts.get(offer_id, 0) + 1

            if vote["customer_id"] == customer["id"]:
                my_vote = offer_id

        deal_status = None
        deal_price = None

        for index, offer in enumerate(offers, start=1):
            offer["label"] = f"Offer {index}"
            offer["notes"] = strip_contact_info(offer.get("notes"))
            offer["votes"] = vote_counts.get(offer["id"], 0)

            if offer["status"] == "Accepted":
                deal_status = offer.get("deal_status") or "Confirmed"
                deal_price = offer["price"]

        return jsonify({
            "success": True,
            "group_status": group_status,
            "deal_status": deal_status,
            "deal_price": deal_price,
            "showroom_price": showroom_price,
            "member_count": group_member_count(group_id),
            "confirmed_count": group_confirmed_count(group_id),
            "my_status": member_status(group_id, customer["id"]),
            "target_members": target_members,
            "my_vote": my_vote,
            "offers": offers
        })

    except Exception as error:
        print("Customer offers error:", str(error))

        return jsonify({
            "success": False,
            "message": "Unable to load offers."
        }), 500


# --------------------------------
# CUSTOMER: VOTE FOR AN OFFER
# One vote per customer per group (can be changed while Open)
# --------------------------------

@app.route("/api/customer/groups/<int:group_id>/vote", methods=["POST"])
def customer_vote_offer(group_id):

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    data = request.get_json(silent=True) or {}

    remove = bool(data.get("remove"))

    try:
        offer_id = None if remove else int(data.get("offer_id"))
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "message": "Select a valid offer."
        }), 400

    try:
        if not customer_in_group(group_id, customer["id"]):
            return jsonify({
                "success": False,
                "message": "Join this group to vote."
            }), 403

        if not customer_in_group(group_id, customer["id"], confirmed_only=True):
            return jsonify({
                "success": False,
                "message": "Your seat must be confirmed before you can vote."
            }), 403

        group = (
            supabase.table("car_groups")
            .select("status")
            .eq("id", group_id)
            .execute()
        ).data

        if not group or group[0]["status"] != "Open":
            return jsonify({
                "success": False,
                "message": "Voting is closed for this group."
            }), 400

        # Customer takes back their vote / agreement
        if remove:
            supabase.table("offer_votes").delete().eq(
                "group_id", group_id
            ).eq("customer_id", customer["id"]).execute()

            return jsonify({
                "success": True,
                "message": "Your response has been removed."
            })

        offer = (
            supabase.table("dealer_offers")
            .select("id")
            .eq("id", offer_id)
            .eq("group_id", group_id)
            .eq("status", "Active")
            .execute()
        ).data

        if not offer:
            return jsonify({
                "success": False,
                "message": "This offer is no longer available."
            }), 404

        supabase.table("offer_votes").upsert({
            "group_id": group_id,
            "offer_id": offer_id,
            "customer_id": customer["id"]
        }, on_conflict="group_id,customer_id").execute()

        return jsonify({
            "success": True,
            "message": "Your vote has been saved."
        })

    except Exception as error:
        print("Vote error:", str(error))

        return jsonify({
            "success": False,
            "message": "Unable to save your vote."
        }), 500


# ================================================================
# ADMIN
# ================================================================

# --------------------------------
# ADMIN LOGIN PAGE + API
# --------------------------------

@app.route("/admin-login")
def admin_login():

    if is_admin_logged_in():
        return redirect(url_for("admin_dashboard"))

    return render_template("admin-login.html")


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():

    data = request.get_json(silent=True) or {}

    username = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({
            "success": False,
            "message": "Enter username and password."
        }), 400

    valid_username = hmac.compare_digest(
        username.encode("utf-8"),
        ADMIN_USERNAME.lower().encode("utf-8")
    )

    valid_password = hmac.compare_digest(
        password.encode("utf-8"),
        ADMIN_PASSWORD.encode("utf-8")
    )

    if not valid_username or not valid_password:
        return jsonify({
            "success": False,
            "message": "Invalid admin username or password."
        }), 401

    clear_admin_session()

    session["admin_id"] = "env-admin"
    session["admin_email"] = ADMIN_USERNAME
    session["is_admin"] = True
    session.permanent = True

    return jsonify({
        "success": True,
        "message": "Admin login successful."
    })


# --------------------------------
# ADMIN DASHBOARD (single page: cars, interests,
# groups, members, dealers, offers)
# --------------------------------

@app.route("/admin-dashboard")
def admin_dashboard():

    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))

    try:
        result = (
            supabase.table("cars")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )

        cars = result.data or []

    except Exception as error:
        print("Car loading error:", str(error))
        cars = []

    return render_template(
        "admin-dashboard.html",
        admin_email=session.get("admin_email"),
        total_cars=len(cars),
        cars=cars,
        vehicle_types=VEHICLE_TYPES,
        cities=CITIES
    )


# Old separate admin pages now open the matching section
# of the single dashboard page.

@app.route("/admin-interests")
def admin_interests():
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))
    return redirect(url_for("admin_dashboard") + "#interests")


@app.route("/admin-groups")
def admin_groups():
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))
    return redirect(url_for("admin_dashboard") + "#groups")


@app.route("/admin-members")
def admin_members():
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))
    return redirect(url_for("admin_dashboard") + "#members")


@app.route("/admin-offers")
def admin_offers():
    if not is_admin_logged_in():
        return redirect(url_for("admin_login"))
    return redirect(url_for("admin_dashboard") + "#offers")


# --------------------------------
# ADMIN: CUSTOMER INTERESTS
# --------------------------------

@app.route("/api/admin/interests", methods=["GET"])
def admin_interests_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        result = (
            supabase.table("car_interest_details")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )

        interests = result.data or []

        # Count interests for each car
        car_counts = {}

        for item in interests:
            car_id = item["car_id"]

            if car_id not in car_counts:
                car_counts[car_id] = {
                    "car_id": car_id,
                    "brand": item.get("brand"),
                    "model": item.get("model"),
                    "variant": item.get("variant"),
                    "interested_count": 0
                }

            car_counts[car_id]["interested_count"] += 1

        return jsonify({
            "success": True,
            "total_interests": len(interests),
            "interests": interests,
            "car_summary": list(car_counts.values())
        })

    except Exception as error:
        print("Admin interests error:", str(error))

        return jsonify({
            "success": False,
            "message": "Unable to load customer interests."
        }), 500


# --------------------------------
# ADMIN: GET AND ADD CARS / BIKES
# --------------------------------

def read_price(value):
    """Empty -> None (price not set). A valid number -> float.
    Anything else -> -1, which car_form_error rejects."""

    raw = str(value or "").replace(",", "").replace("₹", "").strip()

    if not raw:
        return None

    try:
        return float(raw)
    except ValueError:
        return -1


def read_car_form(data):
    return {
        "showroom_price": read_price(data.get("showroom_price")),
        "vehicle_type": str(data.get("vehicle_type") or DEFAULT_VEHICLE_TYPE).strip(),
        "brand": str(data.get("brand") or "").strip(),
        "model": str(data.get("model") or "").strip(),
        "variant": str(data.get("variant") or "").strip(),
        "fuel_type": str(data.get("fuel_type") or "").strip(),
        "transmission": str(data.get("transmission") or "").strip(),
        "image_url": str(data.get("image_url") or "").strip(),
        "description": str(data.get("description") or "").strip()
    }


def car_form_error(car_data):
    """Returns an error message if the vehicle form is not valid, else None."""

    if not car_data["brand"] or not car_data["model"]:
        return "Brand and model are required."

    if car_data["vehicle_type"] not in VEHICLE_TYPES:
        return "Select a valid vehicle type."

    price = car_data.get("showroom_price")

    if price is not None and price <= 0:
        return "Enter a valid showroom price."

    return None


@app.route("/api/admin/cars", methods=["GET", "POST"])
def admin_cars_api():

    if not is_admin_logged_in():
        return admin_denied()

    if request.method == "GET":
        try:
            query = (
                supabase.table("cars")
                .select("*")
                .order("created_at", desc=True)
            )

            # Optional filter: /api/admin/cars?type=Bike
            vehicle_type = request.args.get("type", "").strip()

            if vehicle_type in VEHICLE_TYPES:
                query = query.eq("vehicle_type", vehicle_type)

            result = query.execute()

            return jsonify({
                "success": True,
                "cars": result.data or [],
                "vehicle_types": VEHICLE_TYPES
            })

        except Exception as error:
            print("Fetch cars error:", str(error))
            return jsonify({
                "success": False,
                "message": "Unable to load cars."
            }), 500

    car_data = read_car_form(request.get_json(silent=True) or {})

    error = car_form_error(car_data)

    if error:
        return jsonify({
            "success": False,
            "message": error
        }), 400

    try:
        supabase.table("cars").insert(car_data).execute()

        return jsonify({
            "success": True,
            "message": car_data["vehicle_type"] + " added successfully."
        })

    except Exception as error:
        print("Add car error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to add vehicle."
        }), 500


# --------------------------------
# ADMIN: EDIT CAR
# --------------------------------

@app.route("/api/admin/cars/<int:car_id>", methods=["PUT"])
def edit_admin_car(car_id):

    if not is_admin_logged_in():
        return admin_denied()

    car_data = read_car_form(request.get_json(silent=True) or {})

    error = car_form_error(car_data)

    if error:
        return jsonify({
            "success": False,
            "message": error
        }), 400

    try:
        result = (
            supabase.table("cars")
            .update(car_data)
            .eq("id", car_id)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Vehicle not found."
            }), 404

        return jsonify({
            "success": True,
            "message": "Vehicle updated successfully."
        })

    except Exception as error:
        print("Edit car error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update vehicle."
        }), 500


# --------------------------------
# ADMIN: DELETE CAR
# --------------------------------

@app.route("/api/admin/cars/<int:car_id>", methods=["DELETE"])
def delete_admin_car(car_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        result = (
            supabase.table("cars")
            .delete()
            .eq("id", car_id)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Vehicle not found."
            }), 404

        return jsonify({
            "success": True,
            "message": "Vehicle deleted successfully."
        })

    except Exception as error:
        print("Delete car error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to delete vehicle."
        }), 500


# --------------------------------
# ADMIN: GROUP MANAGEMENT
# --------------------------------

@app.route("/api/admin/groups", methods=["GET", "POST"])
def admin_groups_api():

    if not is_admin_logged_in():
        return admin_denied()

    # GET: view all groups
    if request.method == "GET":
        try:
            groups = (
                supabase.table("car_groups")
                .select("*")
                .order("created_at", desc=True)
                .execute()
            ).data or []

            # One query for all groups (seats = confirmed + pending holds)
            seats, confirmed = {}, {}
            for row in live_member_rows([g["id"] for g in groups]):
                gid = row["group_id"]
                seats[gid] = seats.get(gid, 0) + 1
                if (row.get("status") or "confirmed") == "confirmed":
                    confirmed[gid] = confirmed.get(gid, 0) + 1

            # One query for the vehicle type of every group
            car_ids = list({g["car_id"] for g in groups if g.get("car_id")})
            types = {}

            if car_ids:
                types = {
                    row["id"]: row.get("vehicle_type") or DEFAULT_VEHICLE_TYPE
                    for row in (
                        supabase.table("cars")
                        .select("id, vehicle_type")
                        .in_("id", car_ids)
                        .execute()
                    ).data or []
                }

            for group in groups:
                gid = group["id"]
                group["member_count"] = seats.get(gid, 0)
                group["confirmed_count"] = confirmed.get(gid, 0)
                group["pending_count"] = seats.get(gid, 0) - confirmed.get(gid, 0)
                group["vehicle_type"] = types.get(
                    group.get("car_id"), DEFAULT_VEHICLE_TYPE
                )

            return jsonify({
                "success": True,
                "groups": groups
            })

        except Exception as error:
            print("Admin groups error:", str(error))
            return jsonify({
                "success": False,
                "message": "Unable to load groups."
            }), 500

    # POST: create a new group
    data = request.get_json(silent=True) or {}

    car_id = data.get("car_id")
    group_name = str(data.get("group_name", "")).strip()

    try:
        target_members = int(data.get("target_members", 10))

        if not car_id or not group_name:
            return jsonify({
                "success": False,
                "message": "Vehicle and group name are required."
            }), 400

        if target_members < 1:
            return jsonify({
                "success": False,
                "message": "Target members must be at least 1."
            }), 400

        car = (
            supabase.table("cars")
            .select("id")
            .eq("id", int(car_id))
            .execute()
        )

        if not car.data:
            return jsonify({
                "success": False,
                "message": "Selected vehicle was not found."
            }), 404

        result = supabase.table("car_groups").insert({
            "car_id": int(car_id),
            "group_name": group_name,
            "target_members": target_members,
            "status": "Open"
        }).execute()

        return jsonify({
            "success": True,
            "message": "Group created successfully!",
            "group": result.data[0] if result.data else None
        })

    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "message": "Invalid vehicle ID or target members."
        }), 400

    except Exception as error:
        print("Group creation error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to create group."
        }), 500


@app.route("/api/admin/groups/<int:group_id>", methods=["DELETE"])
def delete_admin_group(group_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        supabase.table("car_groups").delete().eq(
            "id", group_id
        ).execute()

        return jsonify({
            "success": True,
            "message": "Group deleted successfully."
        })

    except Exception as error:
        print("Group delete error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to delete group."
        }), 500


@app.route("/api/admin/groups/<int:group_id>/members", methods=["GET"])
def admin_group_members_api(group_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        group_result = (
            supabase.table("car_groups")
            .select("*")
            .eq("id", group_id)
            .execute()
        )

        if not group_result.data:
            return jsonify({
                "success": False,
                "message": "Group not found."
            }), 404

        group = group_result.data[0]

        members_result = (
            supabase.table("car_group_members")
            .select("*")
            .eq("group_id", group_id)
            .order("joined_at", desc=True)
            .execute()
        )

        members = []

        for member in (members_result.data or []):
            customer_id = member["customer_id"]

            profile_result = (
                supabase.table("profiles")
                .select("full_name, phone")
                .eq("id", customer_id)
                .execute()
            )

            profile = (
                profile_result.data[0]
                if profile_result.data
                else {}
            )

            email = ""

            try:
                auth_response = (
                    supabase.auth.admin.get_user_by_id(customer_id)
                )
                auth_user = auth_response.user
                email = getattr(auth_user, "email", "") or ""
            except Exception as auth_error:
                print("Customer email loading error:", str(auth_error))

            members.append({
                "id": member["id"],
                "customer_id": customer_id,
                "full_name": profile.get("full_name") or "Customer",
                "email": email,
                "phone": profile.get("phone") or "Not provided",
                "joined_at": member.get("joined_at"),
                "status": display_status(member),
                "hold_expires_at": member.get("hold_expires_at"),
                "verified_by": member.get("verified_by"),
                "verified_at": member.get("verified_at")
            })

        car_result = (
            supabase.table("cars")
            .select("brand, model, variant, vehicle_type")
            .eq("id", group["car_id"])
            .execute()
        )

        car = car_result.data[0] if car_result.data else {}

        target = int(group.get("target_members") or 1)

        confirmed = sum(1 for m in members if m["status"] == "confirmed")
        pending = sum(1 for m in members if m["status"] == "pending")
        member_count = confirmed + pending  # seats taken

        return jsonify({
            "success": True,
            "group": group,
            "car": car,
            "members": members,
            "member_count": member_count,
            "confirmed_count": confirmed,
            "pending_count": pending,
            "target_members": target,
            "progress": min(
                100,
                round((member_count / max(target, 1)) * 100)
            )
        })

    except Exception as error:
        print("Admin group members error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load group members."
        }), 500


# --------------------------------
# ADMIN: VERIFY / REMOVE A MEMBER
# Verify = the buyer is serious, the seat is confirmed for good.
# Remove = the seat is freed (the person can join again later).
# --------------------------------

@app.route(
    "/api/admin/groups/<int:group_id>/members/<int:member_id>/verify",
    methods=["POST"]
)
def admin_verify_member(group_id, member_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        row = (
            supabase.table("car_group_members")
            .select("status, hold_expires_at, customer_id")
            .eq("id", member_id)
            .eq("group_id", group_id)
            .execute()
        ).data

        if not row:
            return jsonify({
                "success": False,
                "message": "Member not found."
            }), 404

        # An expired hold gave its seat away. Re-confirming is allowed only
        # if the group still has a free seat.
        if not is_live_member(row[0]):
            group = (
                supabase.table("car_groups")
                .select("target_members")
                .eq("id", group_id)
                .execute()
            ).data

            target = int(group[0]["target_members"]) if group else 0

            if group_member_count(group_id) >= target:
                return jsonify({
                    "success": False,
                    "message": "The group is full now. Remove someone first."
                }), 409

        result = (
            supabase.table("car_group_members")
            .update({
                "status": "confirmed",
                "hold_expires_at": None,
                "verified_by": session.get("admin_email"),
                "verified_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", member_id)
            .eq("group_id", group_id)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Member not found."
            }), 404

        return jsonify({
            "success": True,
            "message": "Member verified. Seat confirmed."
        })

    except Exception as error:
        print("Admin verify member error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to verify the member."
        }), 500


@app.route(
    "/api/admin/groups/<int:group_id>/members/<int:member_id>",
    methods=["DELETE"]
)
def admin_remove_member(group_id, member_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        found = (
            supabase.table("car_group_members")
            .select("customer_id")
            .eq("id", member_id)
            .eq("group_id", group_id)
            .execute()
        ).data

        if not found:
            return jsonify({
                "success": False,
                "message": "Member not found."
            }), 404

        # A removed member's vote must not keep counting
        supabase.table("offer_votes").delete().eq(
            "group_id", group_id
        ).eq("customer_id", found[0]["customer_id"]).execute()

        supabase.table("car_group_members").delete().eq(
            "id", member_id
        ).execute()

        return jsonify({
            "success": True,
            "message": "Member removed. The seat is free again."
        })

    except Exception as error:
        print("Admin remove member error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to remove the member."
        }), 500


# --------------------------------
# ADMIN: VOTES OF ONE GROUP
# Who voted for which offer, and who has not voted yet.
# --------------------------------

@app.route("/api/admin/groups/<int:group_id>/votes", methods=["GET"])
def admin_group_votes_api(group_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        group_rows = (
            supabase.table("car_groups")
            .select("id, group_name, status, target_members")
            .eq("id", group_id)
            .execute()
        ).data

        if not group_rows:
            return jsonify({
                "success": False,
                "message": "Group not found."
            }), 404

        group = group_rows[0]

        offers = (
            supabase.table("dealer_offers")
            .select("id, price, delivery_days, dealer_name, status, deal_status")
            .eq("group_id", group_id)
            .neq("status", "Withdrawn")
            .order("price")
            .execute()
        ).data or []

        votes = (
            supabase.table("offer_votes")
            .select("*")
            .eq("group_id", group_id)
            .execute()
        ).data or []

        # Only confirmed members can vote
        members = [
            row for row in live_member_rows([group_id])
            if (row.get("status") or "confirmed") == "confirmed"
        ]

        member_ids = [row["customer_id"] for row in members]

        profiles = {}

        if member_ids:
            profile_rows = (
                supabase.table("profiles")
                .select("id, full_name, phone")
                .in_("id", member_ids)
                .execute()
            ).data or []

            profiles = {row["id"]: row for row in profile_rows}

        def person(customer_id, voted_at=None):
            profile = profiles.get(customer_id, {})
            return {
                "customer_id": customer_id,
                "full_name": profile.get("full_name") or "Customer",
                "phone": profile.get("phone") or "Not provided",
                "voted_at": voted_at
            }

        voters_by_offer = {}
        voted_ids = set()

        for vote in votes:
            voted_ids.add(vote["customer_id"])
            voters_by_offer.setdefault(vote["offer_id"], []).append(
                person(vote["customer_id"], vote.get("created_at"))
            )

        offer_rows = []

        for index, offer in enumerate(offers, start=1):
            voters = voters_by_offer.get(offer["id"], [])
            offer_rows.append({
                "id": offer["id"],
                "label": f"Offer {index}",
                "dealer_name": offer.get("dealer_name") or "-",
                "price": offer["price"],
                "delivery_days": offer.get("delivery_days"),
                "status": offer["status"],
                "deal_status": offer.get("deal_status"),
                "vote_count": len(voters),
                "voters": voters
            })

        # Most voted first
        offer_rows.sort(key=lambda row: (-row["vote_count"], float(row["price"])))

        not_voted = [
            person(customer_id)
            for customer_id in member_ids
            if customer_id not in voted_ids
        ]

        return jsonify({
            "success": True,
            "group": group,
            "member_count": len(member_ids),
            "voted_count": len(voted_ids & set(member_ids)),
            "offers": offer_rows,
            "not_voted": not_voted
        })

    except Exception as error:
        print("Admin group votes error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load votes."
        }), 500


# ================================================================
# ADMIN: OFFERS (view all, accept one)
# ================================================================

@app.route("/api/admin/offers", methods=["GET"])
def admin_offers_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        offers = (
            supabase.table("dealer_offers")
            .select("*")
            .neq("status", "Withdrawn")
            .order("created_at", desc=True)
            .execute()
        ).data or []

        votes = (
            supabase.table("offer_votes")
            .select("offer_id")
            .execute()
        ).data or []

        vote_counts = {}

        for vote in votes:
            vote_counts[vote["offer_id"]] = (
                vote_counts.get(vote["offer_id"], 0) + 1
            )

        for offer in offers:
            group = (
                supabase.table("car_groups")
                .select("group_name, status, target_members")
                .eq("id", offer["group_id"])
                .execute()
            ).data

            offer["business_name"] = offer.get("dealer_name") or "-"
            offer["group_name"] = group[0]["group_name"] if group else "-"
            offer["group_status"] = group[0]["status"] if group else "-"
            offer["target_members"] = group[0]["target_members"] if group else 0
            offer["votes"] = vote_counts.get(offer["id"], 0)
            offer["member_count"] = group_confirmed_count(offer["group_id"])

        return jsonify({"success": True, "offers": offers})

    except Exception as error:
        print("Admin offers error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load offers."
        }), 500


# --------------------------------
# ADMIN: CREATE / EDIT / DELETE OFFERS
# Admin enters the offers himself. They show in the customer's
# offers page (without any dealer name).
# --------------------------------

def read_offer_form(data):
    """Returns (values, error_message)."""

    try:
        price = float(data.get("price"))
        delivery_raw = data.get("delivery_days")
        delivery_days = (
            int(delivery_raw) if str(delivery_raw or "").strip() else None
        )
    except (ValueError, TypeError):
        return None, "Enter a valid price and delivery days."

    if price <= 0:
        return None, "Price must be greater than zero."

    if delivery_days is not None and delivery_days < 0:
        return None, "Delivery days cannot be negative."

    return {
        "price": price,
        "delivery_days": delivery_days,
        "notes": str(data.get("notes") or "").strip()[:500],
        # Only admin sees this name. Customers never do.
        "dealer_name": str(data.get("dealer_name") or "").strip()[:120]
    }, None


@app.route("/api/admin/offers", methods=["POST"])
def admin_create_offer():

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}

    try:
        group_id = int(data.get("group_id"))
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "message": "Select a group."
        }), 400

    values, error = read_offer_form(data)

    if error:
        return jsonify({"success": False, "message": error}), 400

    try:
        group = (
            supabase.table("car_groups")
            .select("status")
            .eq("id", group_id)
            .execute()
        ).data

        if not group:
            return jsonify({
                "success": False,
                "message": "Group not found."
            }), 404

        if group[0]["status"] != "Open":
            return jsonify({
                "success": False,
                "message": "This group is closed. Offers cannot be added."
            }), 400

        supabase.table("dealer_offers").insert({
            "group_id": group_id,
            "status": "Active",
            **values
        }).execute()

        return jsonify({
            "success": True,
            "message": "Offer added. Customers can see it now."
        })

    except Exception as error:
        print("Admin create offer error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to add the offer."
        }), 500


@app.route("/api/admin/offers/<int:offer_id>", methods=["PUT", "DELETE"])
def admin_edit_offer(offer_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        found = (
            supabase.table("dealer_offers")
            .select("id, status, group_id")
            .eq("id", offer_id)
            .execute()
        ).data

        if not found:
            return jsonify({
                "success": False,
                "message": "Offer not found."
            }), 404

        offer = found[0]

        if offer["status"] == "Accepted":
            return jsonify({
                "success": False,
                "message": "A confirmed deal cannot be changed."
            }), 400

        # ---------- DELETE ----------
        if request.method == "DELETE":
            supabase.table("dealer_offers").update(
                {"status": "Withdrawn"}
            ).eq("id", offer_id).execute()

            supabase.table("offer_votes").delete().eq(
                "offer_id", offer_id
            ).execute()

            return jsonify({
                "success": True,
                "message": "Offer removed."
            })

        # ---------- PUT ----------
        values, error = read_offer_form(request.get_json(silent=True) or {})

        if error:
            return jsonify({"success": False, "message": error}), 400

        supabase.table("dealer_offers").update(
            values
        ).eq("id", offer_id).execute()

        return jsonify({
            "success": True,
            "message": "Offer updated."
        })

    except Exception as error:
        print("Admin edit offer error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the offer."
        }), 500


def close_group_with_offer(group_id, winning_offer_id):
    """
    Final step of a deal: every other live offer in the group is
    rejected (dealers see "Deal closed") and the group is closed.
    """

    supabase.table("dealer_offers").update(
        {"status": "Rejected"}
    ).eq("group_id", group_id).neq(
        "id", winning_offer_id
    ).in_("status", ["Active", "Selected", "Countered"]).execute()

    supabase.table("car_groups").update(
        {"status": "Closed"}
    ).eq("id", group_id).execute()


@app.route("/api/admin/offers/<int:offer_id>/action", methods=["POST"])
def admin_offer_action(offer_id):
    """
    accept   Active -> Accepted (admin confirms the deal, group is closed)
    """

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "")

    try:
        found = (
            supabase.table("dealer_offers")
            .select("*")
            .eq("id", offer_id)
            .execute()
        ).data

        if not found:
            return jsonify({
                "success": False,
                "message": "Offer not found."
            }), 404

        offer = found[0]
        group_id = offer["group_id"]

        group = (
            supabase.table("car_groups")
            .select("status")
            .eq("id", group_id)
            .execute()
        ).data

        if not group or group[0]["status"] != "Open":
            return jsonify({
                "success": False,
                "message": "This group is already closed."
            }), 400

        if action == "accept":
            if offer["status"] not in ("Active", "Selected"):
                return jsonify({
                    "success": False,
                    "message": "Only active offers can be confirmed."
                }), 400

            supabase.table("dealer_offers").update({
                "status": "Accepted",
                "deal_status": "Confirmed",
                "deal_updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", offer_id).execute()

            close_group_with_offer(group_id, offer_id)

            return jsonify({
                "success": True,
                "message": "Offer confirmed and group closed."
            })

        return jsonify({
            "success": False,
            "message": "Unknown action."
        }), 400

    except Exception as error:
        print("Admin offer action error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the offer."
        }), 500


@app.route("/api/admin/offers/<int:offer_id>/deal-status", methods=["POST"])
def admin_deal_status(offer_id):

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}
    deal_status = str(data.get("deal_status") or "")

    if deal_status not in DEAL_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid deal status."
        }), 400

    try:
        result = (
            supabase.table("dealer_offers")
            .update({
                "deal_status": deal_status,
                "deal_updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", offer_id)
            .eq("status", "Accepted")
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Only confirmed deals can be updated."
            }), 404

        message = "Deal status updated to " + deal_status + "."

        # Delivered = purchase is over, so the group moves to Completed
        # automatically (members see it under Past deals).
        group_completed = False

        if deal_status == "Delivered":
            supabase.table("car_groups").update({
                "status": "Completed",
                "completed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", result.data[0]["group_id"]).eq(
                "status", "Closed"
            ).execute()

            group_completed = True
            message = "Vehicle delivered. Group completed."

        return jsonify({
            "success": True,
            "group_completed": group_completed,
            "message": message
        })

    except Exception as error:
        print("Admin deal status error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the deal status."
        }), 500


# ================================================================
# CUSTOMER REVIEWS / TESTIMONIALS
#
# A customer rates a delivered purchase from the profile page.
#   private   no consent to show it publicly (only the team sees it)
#   pending   consent given, waiting for admin approval
#   approved  shown on the landing page (admin approved)
#   hidden    admin hid it
# Only approved reviews with consent are ever public. Contact details in
# comments are hidden, dealer names are never included.
# Run cartogether_reviews.sql once.
# ================================================================

REVIEW_STATUSES = ["private", "pending", "approved", "hidden"]
MIN_REVIEWS_FOR_AVERAGE = 3


def public_display_name(full_name):
    """'Ravi Kumar' -> 'Ravi K.'  (first name and last initial only)"""

    parts = str(full_name or "").split()

    if not parts:
        return "Customer"

    if len(parts) == 1:
        return parts[0]

    return parts[0] + " " + parts[-1][0].upper() + "."


def enrich_reviews(rows, admin=False):
    """Adds car, saving and customer name to review rows (batched queries)."""

    if not rows:
        return []

    group_ids = list({row["group_id"] for row in rows})

    groups = {
        row["id"]: row
        for row in (
            supabase.table("car_groups")
            .select("id, car_id, group_name, target_members")
            .in_("id", group_ids)
            .execute()
        ).data or []
    }

    car_ids = list({g["car_id"] for g in groups.values() if g.get("car_id")})
    cars = {
        row["id"]: row
        for row in (
            supabase.table("cars")
            .select("id, brand, model, variant, showroom_price")
            .in_("id", car_ids)
            .execute()
        ).data or []
    } if car_ids else {}

    offers = {
        row["group_id"]: row
        for row in (
            supabase.table("dealer_offers")
            .select("group_id, price")
            .in_("group_id", group_ids)
            .eq("status", "Accepted")
            .execute()
        ).data or []
    }

    customer_ids = list({row["customer_id"] for row in rows})
    names = {
        row["id"]: row
        for row in (
            supabase.table("profiles")
            .select("id, full_name, phone")
            .in_("id", customer_ids)
            .execute()
        ).data or []
    }

    result = []

    for row in rows:
        group = groups.get(row["group_id"]) or {}
        car = cars.get(group.get("car_id")) or {}
        offer = offers.get(row["group_id"]) or {}
        profile = names.get(row["customer_id"]) or {}

        item = {
            "stars": row["stars"],
            "comment": row.get("comment") or "",
            "name": public_display_name(profile.get("full_name")),
            "car": " ".join(
                part for part in [car.get("brand"), car.get("model")] if part
            ),
            "saving": saving_amount(car.get("showroom_price"), offer.get("price")),
            "group_size": group.get("target_members")
        }

        if admin:
            item.update({
                "id": row["id"],
                "group_name": group.get("group_name"),
                "full_name": profile.get("full_name") or "Customer",
                "phone": profile.get("phone") or "",
                "public_consent": bool(row.get("public_consent")),
                "status": row.get("status"),
                "created_at": row.get("created_at")
            })

        result.append(item)

    return result


def load_public_testimonials(limit=6):
    """(approved reviews for the landing page, rating summary or None)"""

    # Only approved reviews count, so the average matches what visitors see
    approved = (
        supabase.table("purchase_ratings")
        .select("*")
        .eq("status", "approved")
        .eq("public_consent", True)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    stars = [int(row["stars"]) for row in approved if row.get("stars")]

    # An average from one or two reviews is not shown
    summary = (
        {"average": round(sum(stars) / len(stars), 1), "count": len(stars)}
        if len(stars) >= MIN_REVIEWS_FOR_AVERAGE else None
    )

    return enrich_reviews(approved[:limit]), summary


@app.route("/api/customer/groups/<int:group_id>/review", methods=["POST"])
def customer_submit_review(group_id):

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    data = request.get_json(silent=True) or {}

    try:
        stars = int(data.get("stars"))
    except (ValueError, TypeError):
        stars = 0

    if stars < 1 or stars > 5:
        return jsonify({
            "success": False,
            "message": "Please choose 1 to 5 stars."
        }), 400

    comment = strip_contact_info(
        str(data.get("comment") or "").strip()[:600]
    )
    consent = bool(data.get("public_consent"))

    try:
        if not customer_in_group(group_id, customer["id"], confirmed_only=True):
            return jsonify({
                "success": False,
                "message": "Only confirmed members can review this deal."
            }), 403

        group = (
            supabase.table("car_groups")
            .select("status")
            .eq("id", group_id)
            .execute()
        ).data

        delivered = bool(group) and group[0]["status"] == "Completed"

        if group and not delivered:
            delivered = bool((
                supabase.table("dealer_offers")
                .select("id")
                .eq("group_id", group_id)
                .eq("status", "Accepted")
                .eq("deal_status", "Delivered")
                .execute()
            ).data)

        if not delivered:
            return jsonify({
                "success": False,
                "message": "You can review after your vehicle is delivered."
            }), 400

        existing = (
            supabase.table("purchase_ratings")
            .select("id, status")
            .eq("group_id", group_id)
            .eq("customer_id", customer["id"])
            .execute()
        ).data

        # A review the admin hid stays hidden, even if the customer edits it
        if existing and existing[0]["status"] == "hidden":
            status = "hidden"
        else:
            status = "pending" if consent else "private"

        values = {
            "stars": stars,
            "comment": comment or None,
            "public_consent": consent,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        if existing:
            supabase.table("purchase_ratings").update(values).eq(
                "id", existing[0]["id"]
            ).execute()
        else:
            values.update({
                "group_id": group_id,
                "customer_id": customer["id"]
            })
            supabase.table("purchase_ratings").insert(values).execute()

        return jsonify({
            "success": True,
            "status": status,
            "message": "Thank you for your review!"
        })

    except Exception as error:
        print("Submit review error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to save your review. Please try again."
        }), 500


@app.route("/api/admin/reviews", methods=["GET"])
def admin_reviews_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        rows = (
            supabase.table("purchase_ratings")
            .select("*")
            .order("created_at", desc=True)
            .limit(300)
            .execute()
        ).data or []

        return jsonify({
            "success": True,
            "reviews": enrich_reviews(rows, admin=True)
        })

    except Exception as error:
        print("Admin reviews error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load reviews."
        }), 500


@app.route("/api/admin/reviews/<int:review_id>", methods=["POST"])
def admin_review_status(review_id):
    """approve / hide / pending. Approving needs the customer's consent."""

    if not is_admin_logged_in():
        return admin_denied()

    status = str((request.get_json(silent=True) or {}).get("status") or "")

    if status not in ("approved", "hidden", "pending"):
        return jsonify({
            "success": False,
            "message": "Invalid status."
        }), 400

    try:
        found = (
            supabase.table("purchase_ratings")
            .select("public_consent")
            .eq("id", review_id)
            .execute()
        ).data

        if not found:
            return jsonify({
                "success": False,
                "message": "Review not found."
            }), 404

        if status in ("approved", "pending") and not found[0]["public_consent"]:
            return jsonify({
                "success": False,
                "message": "This customer did not agree to show the review publicly."
            }), 400

        supabase.table("purchase_ratings").update({
            "status": status
        }).eq("id", review_id).execute()

        return jsonify({
            "success": True,
            "message": "Review " + status + "."
        })

    except Exception as error:
        print("Admin review status error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the review."
        }), 500


# ================================================================
# ADMIN: DEALS & REVENUE
# Every confirmed deal (group Closed or Completed) with the vehicle,
# final price, customers, savings against the showroom price and the
# commission you earn from the dealer.
# Run cartogether_commission.sql once (commission columns).
# ================================================================

COMMISSION_STATUSES = ["Pending", "Received"]


@app.route("/api/admin/deals", methods=["GET"])
def admin_deals_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        groups = (
            supabase.table("car_groups")
            .select("*")
            .in_("status", ["Closed", "Completed"])
            .execute()
        ).data or []

        if not groups:
            return jsonify({"success": True, "deals": []})

        group_ids = [g["id"] for g in groups]
        car_ids = list({g["car_id"] for g in groups if g.get("car_id")})

        cars = {
            row["id"]: row
            for row in (
                supabase.table("cars")
                .select("id, brand, model, variant, vehicle_type, showroom_price")
                .in_("id", car_ids)
                .execute()
            ).data or []
        } if car_ids else {}

        offers = {
            row["group_id"]: row
            for row in (
                supabase.table("dealer_offers")
                .select("*")
                .in_("group_id", group_ids)
                .eq("status", "Accepted")
                .execute()
            ).data or []
        }

        # Confirmed members of every deal, with name and phone (admin only)
        member_rows = [
            row for row in (
                supabase.table("car_group_members")
                .select("group_id, customer_id, status, hold_expires_at")
                .in_("group_id", group_ids)
                .execute()
            ).data or []
            if is_live_member(row)
            and (row.get("status") or "confirmed") == "confirmed"
        ]

        customer_ids = list({row["customer_id"] for row in member_rows})
        profiles = {}

        if customer_ids:
            profiles = {
                row["id"]: row
                for row in (
                    supabase.table("profiles")
                    .select("id, full_name, phone")
                    .in_("id", customer_ids)
                    .execute()
                ).data or []
            }

        members_by_group = {}
        for row in member_rows:
            profile = profiles.get(row["customer_id"]) or {}
            members_by_group.setdefault(row["group_id"], []).append({
                "name": profile.get("full_name") or "Customer",
                "phone": profile.get("phone") or ""
            })

        deals = []

        for group in groups:
            offer = offers.get(group["id"])

            if not offer:
                continue

            car = cars.get(group.get("car_id")) or {}
            members = members_by_group.get(group["id"], [])
            seats = len(members)

            price = float(offer.get("price") or 0)
            showroom = float(car.get("showroom_price") or 0)
            saving = saving_amount(showroom, price)

            deal_status = offer.get("deal_status") or "Confirmed"
            delivered = group["status"] == "Completed" or deal_status == "Delivered"

            deals.append({
                "offer_id": offer["id"],
                "group_id": group["id"],
                "group_name": group.get("group_name"),
                "group_status": group["status"],
                "car_id": group.get("car_id"),
                "car": {
                    "brand": car.get("brand"),
                    "model": car.get("model"),
                    "variant": car.get("variant"),
                    "vehicle_type": car.get("vehicle_type") or DEFAULT_VEHICLE_TYPE
                },
                "dealer_name": offer.get("dealer_name"),
                "price": price,
                "delivery_days": offer.get("delivery_days"),
                "showroom_price": showroom or None,
                "saving_per_vehicle": saving,
                "total_saving": saving * seats if saving else None,
                "deal_value": price * seats,
                "seats": seats,
                "members": members,
                "deal_status": "Delivered" if delivered else deal_status,
                "delivered": delivered,
                "deal_updated_at": offer.get("deal_updated_at"),
                "completed_at": group.get("completed_at"),
                "commission_amount": offer.get("commission_amount"),
                "commission_status": offer.get("commission_status") or "Pending",
                "commission_received_at": offer.get("commission_received_at"),
                "commission_note": offer.get("commission_note")
            })

        # Running deals first, then delivered ones (newest first)
        deals.sort(
            key=lambda d: (
                d["delivered"],
                -(parse_time(d["deal_updated_at"] or d["completed_at"]) or
                  datetime.min.replace(tzinfo=timezone.utc)).timestamp()
            )
        )

        return jsonify({"success": True, "deals": deals})

    except Exception as error:
        print("Admin deals error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load deals."
        }), 500


@app.route("/api/admin/offers/<int:offer_id>/commission", methods=["POST"])
def admin_offer_commission(offer_id):
    """Record the commission the dealer pays for a confirmed deal."""

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}

    raw = str(data.get("commission_amount") or "").replace(",", "").strip()
    status = str(data.get("commission_status") or "Pending")
    note = str(data.get("commission_note") or "").strip()[:500]

    if status not in COMMISSION_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid commission status."
        }), 400

    amount = None

    if raw:
        try:
            amount = float(raw)
        except ValueError:
            amount = -1

        if amount < 0:
            return jsonify({
                "success": False,
                "message": "Enter a valid commission amount."
            }), 400

    if status == "Received" and amount is None:
        return jsonify({
            "success": False,
            "message": "Enter the commission amount before marking it Received."
        }), 400

    try:
        existing = (
            supabase.table("dealer_offers")
            .select("commission_status, commission_received_at")
            .eq("id", offer_id)
            .eq("status", "Accepted")
            .execute()
        ).data

        if not existing:
            return jsonify({
                "success": False,
                "message": "Only confirmed deals can have a commission."
            }), 404

        received_at = existing[0].get("commission_received_at")

        if status == "Received":
            received_at = received_at or datetime.now(timezone.utc).isoformat()
        else:
            received_at = None

        supabase.table("dealer_offers").update({
            "commission_amount": amount,
            "commission_status": status,
            "commission_received_at": received_at,
            "commission_note": note or None
        }).eq("id", offer_id).execute()

        return jsonify({
            "success": True,
            "message": "Commission saved."
        })

    except Exception as error:
        print("Admin commission error:", str(error))

        hint = (
            " Run cartogether_commission.sql in Supabase first."
            if "commission" in str(error).lower() else ""
        )

        return jsonify({
            "success": False,
            "message": "Unable to save the commission." + hint
        }), 500


@app.route("/api/admin/cars/prices", methods=["POST"])
def admin_cars_prices():
    """Update the showroom price of many vehicles at once.
    Body: {"prices": [{"id": 3, "showroom_price": "1250000"}, ...]}
    An empty price clears it."""

    if not is_admin_logged_in():
        return admin_denied()

    items = (request.get_json(silent=True) or {}).get("prices")

    if not isinstance(items, list) or not items:
        return jsonify({
            "success": False,
            "message": "Nothing to update."
        }), 400

    updates = []

    for item in items[:500]:
        try:
            car_id = int(item.get("id"))
        except (ValueError, TypeError, AttributeError):
            return jsonify({
                "success": False,
                "message": "Invalid vehicle in the list."
            }), 400

        price = read_price(item.get("showroom_price"))

        if price is not None and price <= 0:
            return jsonify({
                "success": False,
                "message": "Enter valid prices (numbers above zero)."
            }), 400

        updates.append((car_id, price))

    try:
        for car_id, price in updates:
            supabase.table("cars").update({
                "showroom_price": price
            }).eq("id", car_id).execute()

        return jsonify({
            "success": True,
            "message": str(len(updates)) + " price(s) updated."
        })

    except Exception as error:
        print("Admin bulk price error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the prices."
        }), 500


# ================================================================
# FINANCE LEADS (vehicle loan help)
# The customer asks for a loan offer from the EMI page. Admin passes the
# lead to a partner bank / NBFC and records the commission it pays.
# Free for the customer. The "consent" flag proves they agreed to share
# their details with partners.
# Works for cars, bikes and scooters (vehicle_type).
# ================================================================

FINANCE_STATUSES = ["New", "Contacted", "Applied", "Approved", "Disbursed", "Rejected"]
FINANCE_OPEN = ["New", "Contacted", "Applied", "Approved"]
FINANCE_EMPLOYMENT = ["Salaried", "Self-employed", "Business owner", "Other"]
FINANCE_MAX_OPEN = 2

# Bikes and scooters cost much less than cars, so the lowest allowed price
# is 20,000 (it was 50,000 when only cars were supported).
FINANCE_MIN_PRICE = 20000
FINANCE_MAX_PRICE = 100000000


def calculate_emi(loan, rate_pct, months):
    if loan <= 0 or months <= 0:
        return 0.0

    monthly_rate = rate_pct / 12 / 100

    if monthly_rate == 0:
        return loan / months

    growth = (1 + monthly_rate) ** months
    return loan * monthly_rate * growth / (growth - 1)


@app.route("/api/customer/finance-leads", methods=["GET", "POST"])
def customer_finance_leads_api():

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    # ---------- GET: my requests ----------
    if request.method == "GET":
        try:
            rows = (
                supabase.table("finance_leads")
                .select("id, vehicle_type, loan_amount, emi, tenure_months, status, created_at")
                .eq("customer_id", customer["id"])
                .order("created_at", desc=True)
                .execute()
            ).data or []

            return jsonify({"success": True, "leads": rows})

        except Exception as error:
            print("Customer finance leads error:", str(error))
            return jsonify({
                "success": False,
                "message": "Unable to load your requests."
            }), 500

    # ---------- POST: new request ----------
    data = request.get_json(silent=True) or {}

    try:
        price = float(data.get("car_price"))
        down_pct = float(data.get("down_payment_pct"))
        rate = float(data.get("interest_rate"))
        months = int(data.get("tenure_months"))
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "message": "Enter valid loan details."
        }), 400

    if not (FINANCE_MIN_PRICE <= price <= FINANCE_MAX_PRICE) \
            or not (0 <= down_pct <= 90) \
            or not (0 <= rate <= 30) or not (6 <= months <= 120):
        return jsonify({
            "success": False,
            "message": "Loan details are out of range."
        }), 400

    vehicle_type = clean_vehicle_type(data.get("vehicle_type"))

    if vehicle_type is None:
        return jsonify({
            "success": False,
            "message": "Select a valid vehicle type."
        }), 400

    if data.get("consent") is not True:
        return jsonify({
            "success": False,
            "message": "Please tick the box to allow us to share your details with partner banks."
        }), 400

    phone = re.sub(
        r"[^\d+]", "",
        str(data.get("phone") or customer.get("phone") or "")
    )

    if len(re.sub(r"\D", "", phone)) < 10:
        return jsonify({
            "success": False,
            "message": "Enter a valid phone number."
        }), 400

    employment = str(data.get("employment") or "").strip()

    if employment not in FINANCE_EMPLOYMENT:
        return jsonify({
            "success": False,
            "message": "Select your employment type."
        }), 400

    # The loan amount and EMI are worked out here again, never trusted
    loan = round(price * (1 - down_pct / 100))
    emi = round(calculate_emi(loan, rate, months))

    try:
        open_requests = (
            supabase.table("finance_leads")
            .select("id", count="exact")
            .eq("customer_id", customer["id"])
            .in_("status", FINANCE_OPEN)
            .execute()
        )

        if (open_requests.count or 0) >= FINANCE_MAX_OPEN:
            return jsonify({
                "success": False,
                "message": "You already have a loan request in progress. Our team will contact you."
            }), 400

        supabase.table("finance_leads").insert({
            "customer_id": customer["id"],
            "kind": "loan",
            "vehicle_type": vehicle_type,
            "car_price": price,
            "down_payment_pct": down_pct,
            "loan_amount": loan,
            "interest_rate": rate,
            "tenure_months": months,
            "emi": emi,
            "phone": phone,
            "city": str(data.get("city") or "").strip()[:80],
            "employment": employment,
            "consent": True,
            "status": "New"
        }).execute()

        return jsonify({
            "success": True,
            "message": "Request sent! A loan expert will call you within 24 hours."
        })

    except Exception as error:
        print("Create finance lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to send your request. Please try again."
        }), 500


@app.route("/api/admin/finance-leads", methods=["GET"])
def admin_finance_leads_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        rows = (
            supabase.table("finance_leads")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        ).data or []

        customer_ids = list({row["customer_id"] for row in rows})
        profiles = {}

        if customer_ids:
            profiles = {
                row["id"]: row
                for row in (
                    supabase.table("profiles")
                    .select("id, full_name")
                    .in_("id", customer_ids)
                    .execute()
                ).data or []
            }

        for row in rows:
            row["customer_name"] = (
                profiles.get(row["customer_id"], {}).get("full_name") or "Customer"
            )

        return jsonify({"success": True, "leads": rows})

    except Exception as error:
        print("Admin finance leads error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load loan requests."
        }), 500


@app.route("/api/admin/finance-leads/<int:lead_id>", methods=["POST"])
def admin_update_finance_lead(lead_id):

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}

    status = str(data.get("status") or "").strip()

    if status not in FINANCE_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid status."
        }), 400

    commission = None
    raw_commission = str(data.get("commission") or "").strip()

    if raw_commission:
        try:
            commission = float(raw_commission)
        except ValueError:
            commission = -1

        if commission < 0:
            return jsonify({
                "success": False,
                "message": "Enter a valid commission amount."
            }), 400

    try:
        result = (
            supabase.table("finance_leads")
            .update({
                "status": status,
                "partner": str(data.get("partner") or "").strip()[:120],
                "commission": commission,
                "admin_note": str(data.get("admin_note") or "").strip()[:500],
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", lead_id)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Request not found."
            }), 404

        return jsonify({"success": True, "message": "Loan request updated."})

    except Exception as error:
        print("Admin update finance lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the request."
        }), 500


# ================================================================
# CONSULTING (expert consultation booking)
# Free for now. The "fee" column is ready if you decide to charge later.
# ================================================================

CONSULT_STATUSES = ["Requested", "Scheduled", "Completed", "Cancelled"]
CONSULT_MODES = ["Call", "Video", "Chat"]
CONSULT_SLOTS = ["Morning", "Afternoon", "Evening"]
CONSULT_TOPICS = [
    "Which vehicle should I buy?",
    "Compare two or three vehicles",
    "Loan and EMI advice",
    "Understanding group buying offers",
    "Used vehicle / exchange advice",
    "Other",
]
CONSULT_MAX_ACTIVE = 3


def consultation_rows_with_cars(rows):
    """Adds a car name to each consultation row (one query for all cars)."""

    car_ids = list({row["car_id"] for row in rows if row.get("car_id")})
    cars = {}

    if car_ids:
        cars = {
            car["id"]: car
            for car in (
                supabase.table("cars")
                .select("id, brand, model, variant")
                .in_("id", car_ids)
                .execute()
            ).data or []
        }

    for row in rows:
        car = cars.get(row.get("car_id"))
        row["car_name"] = (
            " ".join(
                part for part in
                (car.get("brand"), car.get("model"), car.get("variant"))
                if part
            )
            if car else ""
        )

    return rows


@app.route("/customer/consulting")
def customer_consulting_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    return render_template(
        "customer/consulting.html",
        customer=customer,
        active_page="consulting",
        cars=load_all_cars(),
        topics=CONSULT_TOPICS,
        modes=CONSULT_MODES,
        slots=CONSULT_SLOTS
    )


@app.route("/api/customer/consultations", methods=["GET", "POST"])
def customer_consultations_api():

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    # ---------- GET: my bookings ----------
    if request.method == "GET":
        try:
            rows = (
                supabase.table("consultations")
                .select("*")
                .eq("customer_id", customer["id"])
                .order("created_at", desc=True)
                .execute()
            ).data or []

            return jsonify({
                "success": True,
                "consultations": consultation_rows_with_cars(rows)
            })

        except Exception as error:
            print("Customer consultations error:", str(error))
            return jsonify({
                "success": False,
                "message": "Unable to load your bookings."
            }), 500

    # ---------- POST: new booking ----------
    data = request.get_json(silent=True) or {}

    topic = str(data.get("topic") or "").strip()
    mode = str(data.get("mode") or "Call").strip()
    slot = str(data.get("preferred_slot") or "").strip()
    note = str(data.get("note") or "").strip()[:500]
    date_raw = str(data.get("preferred_date") or "").strip()

    if topic not in CONSULT_TOPICS:
        return jsonify({
            "success": False,
            "message": "Select what you need help with."
        }), 400

    if mode not in CONSULT_MODES:
        return jsonify({
            "success": False,
            "message": "Select Call, Video or Chat."
        }), 400

    if slot and slot not in CONSULT_SLOTS:
        return jsonify({
            "success": False,
            "message": "Select a valid time slot."
        }), 400

    preferred_date = None

    if date_raw:
        try:
            preferred_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({
                "success": False,
                "message": "Enter a valid date."
            }), 400

        if preferred_date < datetime.now(IST).date():
            return jsonify({
                "success": False,
                "message": "Choose today or a later date."
            }), 400

    car_id = None

    if data.get("car_id"):
        try:
            car_id = int(data.get("car_id"))
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "message": "Select a valid vehicle."
            }), 400

    try:
        active = (
            supabase.table("consultations")
            .select("id", count="exact")
            .eq("customer_id", customer["id"])
            .in_("status", ["Requested", "Scheduled"])
            .execute()
        )

        if (active.count or 0) >= CONSULT_MAX_ACTIVE:
            return jsonify({
                "success": False,
                "message": "You already have " + str(CONSULT_MAX_ACTIVE) +
                           " open requests. Please wait for them to finish."
            }), 400

        supabase.table("consultations").insert({
            "customer_id": customer["id"],
            "car_id": car_id,
            "topic": topic,
            "mode": mode,
            "preferred_date": preferred_date.isoformat() if preferred_date else None,
            "preferred_slot": slot or None,
            "note": note,
            "status": "Requested",
            "fee": 0
        }).execute()

        return jsonify({
            "success": True,
            "message": "Request sent! Our expert will contact you soon."
        })

    except Exception as error:
        print("Create consultation error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to send your request. Please try again."
        }), 500


@app.route("/api/customer/consultations/<int:consult_id>/cancel", methods=["POST"])
def customer_cancel_consultation(consult_id):

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    try:
        result = (
            supabase.table("consultations")
            .update({
                "status": "Cancelled",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", consult_id)
            .eq("customer_id", customer["id"])
            .in_("status", ["Requested", "Scheduled"])
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "This request can no longer be cancelled."
            }), 404

        return jsonify({
            "success": True,
            "message": "Your request has been cancelled."
        })

    except Exception as error:
        print("Cancel consultation error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to cancel. Please try again."
        }), 500


# ---------- admin side ----------

@app.route("/api/admin/consultations", methods=["GET"])
def admin_consultations_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        rows = (
            supabase.table("consultations")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        ).data or []

        customer_ids = list({row["customer_id"] for row in rows})
        profiles = {}

        if customer_ids:
            profiles = {
                row["id"]: row
                for row in (
                    supabase.table("profiles")
                    .select("id, full_name, phone")
                    .in_("id", customer_ids)
                    .execute()
                ).data or []
            }

        for row in rows:
            profile = profiles.get(row["customer_id"], {})
            row["customer_name"] = profile.get("full_name") or "Customer"
            row["customer_phone"] = profile.get("phone") or "Not provided"

        return jsonify({
            "success": True,
            "consultations": consultation_rows_with_cars(rows)
        })

    except Exception as error:
        print("Admin consultations error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load consultations."
        }), 500


@app.route("/api/admin/consultations/<int:consult_id>", methods=["POST"])
def admin_update_consultation(consult_id):

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}

    status = str(data.get("status") or "").strip()

    if status not in CONSULT_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid status."
        }), 400

    scheduled_raw = str(data.get("scheduled_at") or "").strip()
    scheduled_at = None

    if scheduled_raw:
        try:
            # <input type="datetime-local"> sends local time without timezone;
            # it is stored as given (treated as the business's local time).
            scheduled_at = datetime.fromisoformat(scheduled_raw).isoformat()
        except ValueError:
            return jsonify({
                "success": False,
                "message": "Enter a valid date and time."
            }), 400

    if status == "Scheduled" and not scheduled_at:
        return jsonify({
            "success": False,
            "message": "Pick the date and time before marking it Scheduled."
        }), 400

    try:
        result = (
            supabase.table("consultations")
            .update({
                "status": status,
                "scheduled_at": scheduled_at,
                "meeting_link": str(data.get("meeting_link") or "").strip()[:300],
                "admin_note": str(data.get("admin_note") or "").strip()[:500],
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", consult_id)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Request not found."
            }), 404

        return jsonify({
            "success": True,
            "message": "Consultation updated."
        })

    except Exception as error:
        print("Admin update consultation error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the consultation."
        }), 500


# =====================================================================
# USED VEHICLE / EXCHANGE LEADS
#
# Flow:  Customer fills the "quote for my old vehicle" form
#        -> admin sees the lead -> passes it to a used vehicle dealer
#        -> admin records the quote and the lead fee the dealer pays.
#
# Free for the customer. The "consent" flag proves they agreed to share
# their details with partner dealers. The dealer name is never sent to
# the customer (same rule as group offers).
# Works for cars, bikes and scooters (vehicle_type).
# =====================================================================

USED_STATUSES = ["New", "Contacted", "Quoted", "Sold", "Closed", "Cancelled"]
USED_OPEN = ["New", "Contacted", "Quoted"]
USED_FUELS = ["Petrol", "Diesel", "CNG", "Electric", "Hybrid"]
USED_TRANSMISSIONS = ["Manual", "Automatic"]
USED_CONDITIONS = ["Excellent", "Good", "Average", "Needs work"]
USED_OWNERS = [1, 2, 3, 4]
USED_MAX_OPEN = 3


def add_group_names(rows):
    """Adds group_name to every lead linked to a group (one query for all)."""

    group_ids = list({row["group_id"] for row in rows if row.get("group_id")})
    names = {}

    if group_ids:
        names = {
            group["id"]: group["group_name"]
            for group in (
                supabase.table("car_groups")
                .select("id, group_name")
                .in_("id", group_ids)
                .execute()
            ).data or []
        }

    for row in rows:
        row["group_name"] = names.get(row.get("group_id"), "")

    return rows


@app.route("/api/customer/used-car-leads", methods=["GET", "POST"])
def customer_used_car_leads_api():

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    # ---------- GET: my requests ----------
    if request.method == "GET":
        try:
            rows = (
                supabase.table("used_car_leads")
                .select("id, group_id, vehicle_type, brand, model, variant, year, "
                        "km_driven, expected_price, quote_amount, status, created_at")
                .eq("customer_id", customer["id"])
                .order("created_at", desc=True)
                .execute()
            ).data or []

            add_group_names(rows)

            # Dealer / partner name and lead fee are never sent to customers
            return jsonify({"success": True, "leads": rows})

        except Exception as error:
            print("Customer used car leads error:", str(error))
            return jsonify({
                "success": False,
                "message": "Unable to load your requests."
            }), 500

    # ---------- POST: new request ----------
    data = request.get_json(silent=True) or {}

    vehicle_type = clean_vehicle_type(data.get("vehicle_type"))
    brand = str(data.get("brand") or "").strip()[:60]
    model = str(data.get("model") or "").strip()[:60]
    variant = str(data.get("variant") or "").strip()[:60]
    fuel = str(data.get("fuel_type") or "").strip()
    transmission = str(data.get("transmission") or "").strip()
    condition = str(data.get("condition") or "").strip()
    city = str(data.get("city") or "").strip()[:80]
    note = str(data.get("note") or "").strip()[:500]

    if vehicle_type is None:
        return jsonify({
            "success": False,
            "message": "Select a valid vehicle type."
        }), 400

    if not brand or not model:
        return jsonify({
            "success": False,
            "message": "Enter the brand and model of your vehicle."
        }), 400

    try:
        year = int(data.get("year"))
        km_driven = int(data.get("km_driven"))
        owners = int(data.get("owners"))

        expected_raw = str(data.get("expected_price") or "").strip()
        expected_price = float(expected_raw) if expected_raw else None
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "message": "Enter valid year, kilometres and owners."
        }), 400

    this_year = datetime.now(IST).year

    if not (1990 <= year <= this_year):
        return jsonify({
            "success": False,
            "message": "Enter a valid manufacturing year."
        }), 400

    if not (0 <= km_driven <= 1000000):
        return jsonify({
            "success": False,
            "message": "Enter valid kilometres driven."
        }), 400

    if owners not in USED_OWNERS:
        return jsonify({
            "success": False,
            "message": "Select number of owners."
        }), 400

    if expected_price is not None and not (10000 <= expected_price <= 100000000):
        return jsonify({
            "success": False,
            "message": "Expected price is out of range."
        }), 400

    if fuel not in USED_FUELS:
        return jsonify({
            "success": False,
            "message": "Select the fuel type."
        }), 400

    if transmission not in USED_TRANSMISSIONS:
        return jsonify({
            "success": False,
            "message": "Select the transmission."
        }), 400

    if condition not in USED_CONDITIONS:
        return jsonify({
            "success": False,
            "message": "Select the vehicle condition."
        }), 400

    if not city:
        return jsonify({
            "success": False,
            "message": "Enter your city."
        }), 400

    if data.get("consent") is not True:
        return jsonify({
            "success": False,
            "message": "Please tick the box to allow us to share your details with partner dealers."
        }), 400

    phone = re.sub(
        r"[^\d+]", "",
        str(data.get("phone") or customer.get("phone") or "")
    )

    if len(re.sub(r"\D", "", phone)) < 10:
        return jsonify({
            "success": False,
            "message": "Enter a valid phone number."
        }), 400

    # Optional: the group the customer is buying a new vehicle through
    group_id = None

    if data.get("group_id"):
        try:
            group_id = int(data.get("group_id"))
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "message": "Select a valid group."
            }), 400

    try:
        if group_id is not None and not customer_in_group(group_id, customer["id"]):
            return jsonify({
                "success": False,
                "message": "You can only link a group you have joined."
            }), 403

        open_requests = (
            supabase.table("used_car_leads")
            .select("id", count="exact")
            .eq("customer_id", customer["id"])
            .in_("status", USED_OPEN)
            .execute()
        )

        if (open_requests.count or 0) >= USED_MAX_OPEN:
            return jsonify({
                "success": False,
                "message": "You already have " + str(USED_MAX_OPEN) +
                           " requests in progress. Our team will contact you."
            }), 400

        supabase.table("used_car_leads").insert({
            "customer_id": customer["id"],
            "group_id": group_id,
            "vehicle_type": vehicle_type,
            "brand": brand,
            "model": model,
            "variant": variant,
            "year": year,
            "km_driven": km_driven,
            "owners": owners,
            "fuel_type": fuel,
            "transmission": transmission,
            "condition": condition,
            "expected_price": expected_price,
            "city": city,
            "phone": phone,
            "note": note,
            "consent": True,
            "status": "New"
        }).execute()

        return jsonify({
            "success": True,
            "message": "Request sent! You will get a quote for your vehicle within 24 hours."
        })

    except Exception as error:
        print("Create used car lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to send your request. Please try again."
        }), 500


@app.route("/api/customer/used-car-leads/<int:lead_id>/cancel", methods=["POST"])
def customer_cancel_used_car_lead(lead_id):

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    try:
        result = (
            supabase.table("used_car_leads")
            .update({
                "status": "Cancelled",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", lead_id)
            .eq("customer_id", customer["id"])
            .in_("status", USED_OPEN)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "This request can no longer be cancelled."
            }), 404

        return jsonify({
            "success": True,
            "message": "Your request has been cancelled."
        })

    except Exception as error:
        print("Cancel used car lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to cancel. Please try again."
        }), 500


# ---------- admin side ----------

@app.route("/api/admin/used-car-leads", methods=["GET"])
def admin_used_car_leads_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        rows = (
            supabase.table("used_car_leads")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        ).data or []

        customer_ids = list({row["customer_id"] for row in rows})
        profiles = {}

        if customer_ids:
            profiles = {
                row["id"]: row
                for row in (
                    supabase.table("profiles")
                    .select("id, full_name")
                    .in_("id", customer_ids)
                    .execute()
                ).data or []
            }

        for row in rows:
            row["customer_name"] = (
                profiles.get(row["customer_id"], {}).get("full_name") or "Customer"
            )

        add_group_names(rows)

        return jsonify({"success": True, "leads": rows})

    except Exception as error:
        print("Admin used car leads error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load used vehicle requests."
        }), 500


@app.route("/api/admin/used-car-leads/<int:lead_id>", methods=["POST"])
def admin_update_used_car_lead(lead_id):

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}

    status = str(data.get("status") or "").strip()

    if status not in USED_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid status."
        }), 400

    amounts = {}

    # quote_amount = price the dealer offered the customer
    # lead_fee     = money the dealer pays you for this lead
    for key in ("quote_amount", "lead_fee"):
        raw = str(data.get(key) or "").strip()

        if not raw:
            amounts[key] = None
            continue

        try:
            value = float(raw)
        except ValueError:
            value = -1

        if value < 0:
            return jsonify({
                "success": False,
                "message": "Enter valid amounts."
            }), 400

        amounts[key] = value

    if status == "Quoted" and amounts["quote_amount"] is None:
        return jsonify({
            "success": False,
            "message": "Enter the quote amount before marking it Quoted."
        }), 400

    try:
        result = (
            supabase.table("used_car_leads")
            .update({
                "status": status,
                "partner": str(data.get("partner") or "").strip()[:120],
                "quote_amount": amounts["quote_amount"],
                "lead_fee": amounts["lead_fee"],
                "admin_note": str(data.get("admin_note") or "").strip()[:500],
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", lead_id)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Request not found."
            }), 404

        return jsonify({"success": True, "message": "Used vehicle request updated."})

    except Exception as error:
        print("Admin update used car lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the request."
        }), 500


# =====================================================================
# VEHICLE INSURANCE LEADS
#
# Flow:  Customer asks for an insurance quote (new vehicle or renewal)
#        -> admin passes the lead to a LICENSED insurance partner
#        -> partner calls the customer and issues the policy
#        -> admin records the commission the partner pays.
#
# The website never shows premiums or sells a policy itself. The licensed
# partner does that. Free for the customer. The "consent" flag proves they
# agreed to share their details with the partner.
# Works for cars, bikes and scooters (vehicle_type). Make sure your partner
# is licensed for two-wheeler policies too.
# =====================================================================

INS_STATUSES = ["New", "Contacted", "Quoted", "Issued", "Rejected", "Cancelled"]
INS_OPEN = ["New", "Contacted", "Quoted"]
INS_KINDS = ["New car", "Renewal"]  # "New car" is kept so old rows still work; it means "new vehicle"
INS_FUELS = ["Petrol", "Diesel", "CNG", "Electric", "Hybrid"]
INS_NCB = ["Yes", "No", "Not sure"]
INS_MAX_OPEN = 3


def parse_ins_date(raw):
    """'YYYY-MM-DD' -> date, or None if empty. Raises ValueError if invalid."""

    raw = str(raw or "").strip()

    if not raw:
        return None

    return datetime.strptime(raw, "%Y-%m-%d").date()


@app.route("/api/customer/insurance-leads", methods=["GET", "POST"])
def customer_insurance_leads_api():

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    # ---------- GET: my requests ----------
    if request.method == "GET":
        try:
            rows = (
                supabase.table("insurance_leads")
                .select("id, group_id, vehicle_type, kind, brand, model, variant, "
                        "year, prev_expiry, status, created_at")
                .eq("customer_id", customer["id"])
                .order("created_at", desc=True)
                .execute()
            ).data or []

            add_group_names(rows)

            # Partner name and commission are never sent to customers
            return jsonify({"success": True, "leads": rows})

        except Exception as error:
            print("Customer insurance leads error:", str(error))
            return jsonify({
                "success": False,
                "message": "Unable to load your requests."
            }), 500

    # ---------- POST: new request ----------
    data = request.get_json(silent=True) or {}

    vehicle_type = clean_vehicle_type(data.get("vehicle_type"))
    kind = str(data.get("kind") or "").strip()
    brand = str(data.get("brand") or "").strip()[:60]
    model = str(data.get("model") or "").strip()[:60]
    variant = str(data.get("variant") or "").strip()[:60]
    fuel = str(data.get("fuel_type") or "").strip()
    ncb = str(data.get("ncb") or "Not sure").strip()
    city = str(data.get("city") or "").strip()[:80]
    note = str(data.get("note") or "").strip()[:500]

    if vehicle_type is None:
        return jsonify({
            "success": False,
            "message": "Select a valid vehicle type."
        }), 400

    if kind not in INS_KINDS:
        return jsonify({
            "success": False,
            "message": "Select new vehicle insurance or renewal."
        }), 400

    if not brand or not model:
        return jsonify({
            "success": False,
            "message": "Enter the brand and model of your vehicle."
        }), 400

    if fuel not in INS_FUELS:
        return jsonify({
            "success": False,
            "message": "Select the fuel type."
        }), 400

    if ncb not in INS_NCB:
        return jsonify({
            "success": False,
            "message": "Select the no claim bonus option."
        }), 400

    if not city:
        return jsonify({
            "success": False,
            "message": "Enter your city."
        }), 400

    today = datetime.now(IST).date()

    year = None
    year_raw = str(data.get("year") or "").strip()

    if year_raw:
        try:
            year = int(year_raw)
        except ValueError:
            year = -1

        if not (1990 <= year <= today.year + 1):
            return jsonify({
                "success": False,
                "message": "Enter a valid manufacturing year."
            }), 400

    # Registration number is optional. Letters and numbers only.
    reg_number = re.sub(r"[^A-Z0-9]", "", str(data.get("reg_number") or "").upper())

    if reg_number and not (6 <= len(reg_number) <= 13):
        return jsonify({
            "success": False,
            "message": "Enter a valid registration number, or leave it empty."
        }), 400

    try:
        prev_expiry = parse_ins_date(data.get("prev_expiry"))
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Enter a valid policy expiry date."
        }), 400

    if kind == "Renewal":
        if not prev_expiry:
            return jsonify({
                "success": False,
                "message": "Enter when your current policy expires."
            }), 400

        # A policy that ended up to 3 years ago can still be renewed (lapsed)
        if not (today - timedelta(days=1095) <= prev_expiry <= today + timedelta(days=730)):
            return jsonify({
                "success": False,
                "message": "The expiry date looks wrong. Please check it."
            }), 400
    else:
        prev_expiry = None

    if data.get("consent") is not True:
        return jsonify({
            "success": False,
            "message": "Please tick the box to allow us to share your details with our insurance partner."
        }), 400

    phone = re.sub(
        r"[^\d+]", "",
        str(data.get("phone") or customer.get("phone") or "")
    )

    if len(re.sub(r"\D", "", phone)) < 10:
        return jsonify({
            "success": False,
            "message": "Enter a valid phone number."
        }), 400

    # Optional: the group the customer is buying a new vehicle through
    group_id = None

    if data.get("group_id"):
        try:
            group_id = int(data.get("group_id"))
        except (ValueError, TypeError):
            return jsonify({
                "success": False,
                "message": "Select a valid group."
            }), 400

    try:
        if group_id is not None and not customer_in_group(group_id, customer["id"]):
            return jsonify({
                "success": False,
                "message": "You can only link a group you have joined."
            }), 403

        open_requests = (
            supabase.table("insurance_leads")
            .select("id", count="exact")
            .eq("customer_id", customer["id"])
            .in_("status", INS_OPEN)
            .execute()
        )

        if (open_requests.count or 0) >= INS_MAX_OPEN:
            return jsonify({
                "success": False,
                "message": "You already have " + str(INS_MAX_OPEN) +
                           " insurance requests in progress. Our partner will contact you."
            }), 400

        supabase.table("insurance_leads").insert({
            "customer_id": customer["id"],
            "group_id": group_id,
            "vehicle_type": vehicle_type,
            "kind": kind,
            "brand": brand,
            "model": model,
            "variant": variant,
            "year": year,
            "fuel_type": fuel,
            "reg_number": reg_number,
            "prev_expiry": prev_expiry.isoformat() if prev_expiry else None,
            "ncb": ncb,
            "city": city,
            "phone": phone,
            "note": note,
            "consent": True,
            "status": "New"
        }).execute()

        return jsonify({
            "success": True,
            "message": "Request sent! Our insurance partner will call you within 24 hours."
        })

    except Exception as error:
        print("Create insurance lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to send your request. Please try again."
        }), 500


@app.route("/api/customer/insurance-leads/<int:lead_id>/cancel", methods=["POST"])
def customer_cancel_insurance_lead(lead_id):

    customer = get_logged_in_customer()

    if not customer:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    try:
        result = (
            supabase.table("insurance_leads")
            .update({
                "status": "Cancelled",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", lead_id)
            .eq("customer_id", customer["id"])
            .in_("status", INS_OPEN)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "This request can no longer be cancelled."
            }), 404

        return jsonify({
            "success": True,
            "message": "Your request has been cancelled."
        })

    except Exception as error:
        print("Cancel insurance lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to cancel. Please try again."
        }), 500


# ---------- admin side ----------

@app.route("/api/admin/insurance-leads", methods=["GET"])
def admin_insurance_leads_api():

    if not is_admin_logged_in():
        return admin_denied()

    try:
        rows = (
            supabase.table("insurance_leads")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        ).data or []

        customer_ids = list({row["customer_id"] for row in rows})
        profiles = {}

        if customer_ids:
            profiles = {
                row["id"]: row
                for row in (
                    supabase.table("profiles")
                    .select("id, full_name")
                    .in_("id", customer_ids)
                    .execute()
                ).data or []
            }

        for row in rows:
            row["customer_name"] = (
                profiles.get(row["customer_id"], {}).get("full_name") or "Customer"
            )

        add_group_names(rows)

        return jsonify({"success": True, "leads": rows})

    except Exception as error:
        print("Admin insurance leads error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to load insurance requests."
        }), 500


@app.route("/api/admin/insurance-leads/<int:lead_id>", methods=["POST"])
def admin_update_insurance_lead(lead_id):

    if not is_admin_logged_in():
        return admin_denied()

    data = request.get_json(silent=True) or {}

    status = str(data.get("status") or "").strip()

    if status not in INS_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid status."
        }), 400

    commission = None
    raw_commission = str(data.get("commission") or "").strip()

    if raw_commission:
        try:
            commission = float(raw_commission)
        except ValueError:
            commission = -1

        if commission < 0:
            return jsonify({
                "success": False,
                "message": "Enter a valid commission amount."
            }), 400

    try:
        result = (
            supabase.table("insurance_leads")
            .update({
                "status": status,
                "partner": str(data.get("partner") or "").strip()[:120],
                "commission": commission,
                "admin_note": str(data.get("admin_note") or "").strip()[:500],
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", lead_id)
            .execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Request not found."
            }), 404

        return jsonify({"success": True, "message": "Insurance request updated."})

    except Exception as error:
        print("Admin update insurance lead error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to update the request."
        }), 500


@app.route("/customer/sell-car")
def customer_sell_car_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    return render_template(
        "customer/sell_car.html",
        customer=customer,
        active_page="sell",
        vehicle_types=VEHICLE_TYPES
    )


@app.route("/customer/insurance")
def customer_insurance_page():

    customer = get_logged_in_customer()

    if not customer:
        return redirect(url_for("login"))

    return render_template(
        "customer/insurance.html",
        customer=customer,
        active_page="insurance",
        vehicle_types=VEHICLE_TYPES
    )


# --------------------------------
# LOGOUT (admin + customer)
# Only logs out when the user clicks Logout.
# --------------------------------

@app.route("/admin-logout", methods=["POST"])
def admin_logout():

    clear_admin_session()

    return redirect(url_for("admin_login"))


@app.route("/logout", methods=["POST"])
def logout():

    clear_customer_session()

    return redirect(url_for("login"))


# =====================================================================
# COMPLETED GROUPS
#
# Flow:  Open  ->  Closed (deal confirmed)  ->  Completed (every vehicle delivered)
# =====================================================================

@app.route("/api/admin/groups/<int:group_id>/complete", methods=["POST"])
def admin_complete_group(group_id):
    """
    Closed -> Completed. Allowed only when the confirmed deal is marked Delivered.
    After this the group moves to "Past deals" on the members' My Deals page.
    """

    if not is_admin_logged_in():
        return admin_denied()

    try:
        rows = (
            supabase.table("car_groups")
            .select("status")
            .eq("id", group_id)
            .execute()
        ).data

        if not rows:
            return jsonify({
                "success": False,
                "message": "Group not found."
            }), 404

        status = rows[0]["status"]

        if status == "Completed":
            return jsonify({
                "success": True,
                "message": "This group is already completed."
            })

        if status != "Closed":
            return jsonify({
                "success": False,
                "message": "Only a closed group (deal confirmed) can be completed."
            }), 400

        deal = (
            supabase.table("dealer_offers")
            .select("id, deal_status")
            .eq("group_id", group_id)
            .eq("status", "Accepted")
            .execute()
        ).data

        if not deal or deal[0].get("deal_status") != "Delivered":
            return jsonify({
                "success": False,
                "message": "Mark the deal as Delivered first."
            }), 400

        # .eq("status", "Closed") makes sure a double click cannot overwrite anything
        supabase.table("car_groups").update({
            "status": "Completed",
            "completed_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", group_id).eq("status", "Closed").execute()

        return jsonify({
            "success": True,
            "message": "Group completed. It now shows under Past deals."
        })

    except Exception as error:
        print("Admin complete group error:", str(error))
        return jsonify({
            "success": False,
            "message": "Unable to complete the group."
        }), 500


# ================================================================
# LOCAL ADS
# ================================================================

AD_PLACEMENTS = ["home", "cars", "emi", "my_groups"]


def clean_url(value):
    """Only http/https links are allowed (blocks javascript: links)."""
    value = str(value or "").strip()
    if value.startswith(("http://", "https://")):
        return value[:500]
    return ""


def get_ads(placement, limit=2):
    """Live ads for one placement. Counts a view for each ad shown.
    Location rule: an ad with no target city shows to everyone. An ad with
    a target city shows only to customers who picked that city."""

    try:
        today = datetime.now(IST).date().isoformat()
        my_city = str(session.get("cached_city") or "").strip().lower()

        rows = (
            supabase.table("ads")
            .select("id, advertiser_name, title, description, image_url, "
                    "start_date, end_date, target_city")
            .eq("placement", placement)
            .eq("is_active", True)
            .execute()
        ).data or []

        def city_ok(row):
            target = str(row.get("target_city") or "").strip().lower()
            return not target or target == my_city

        live = [
            row for row in rows
            if not (row.get("start_date") and row["start_date"] > today)
            and not (row.get("end_date") and row["end_date"] < today)
            and city_ok(row)
        ]

        picked = random.sample(live, min(limit, len(live)))

        if picked:
            supabase.rpc(
                "increment_ad_views",
                {"p_ids": [ad["id"] for ad in picked]}
            ).execute()

        return picked

    except Exception as error:
        print("Load ads error:", str(error))
        return []


# Makes get_ads() usable inside every template
@app.context_processor
def inject_ads():
    return {"get_ads": get_ads, "cities": CITIES}


@app.route("/ad/<int:ad_id>/click")
def ad_click(ad_id):
    """Counts the click, then sends the visitor to the advertiser."""

    try:
        rows = (
            supabase.table("ads")
            .select("link_url")
            .eq("id", ad_id)
            .execute()
        ).data

        link = clean_url(rows[0]["link_url"]) if rows else ""

        if link:
            supabase.rpc("increment_ad_clicks", {"p_id": ad_id}).execute()
            return redirect(link)

    except Exception as error:
        print("Ad click error:", str(error))

    return redirect(url_for("home"))


def read_ad_form(data):
    """Returns (values, error_message)."""

    advertiser = str(data.get("advertiser_name") or "").strip()[:120]
    title = str(data.get("title") or "").strip()[:80]
    link = clean_url(data.get("link_url"))
    image = clean_url(data.get("image_url"))
    placement = str(data.get("placement") or "").strip()

    if not advertiser or not title:
        return None, "Advertiser name and title are required."

    if not link:
        return None, "Enter a link starting with http:// or https://"

    if placement not in AD_PLACEMENTS:
        return None, "Select where the ad should show."

    target_city = clean_city(data.get("target_city"))

    if target_city is None:
        return None, "Select a valid city, or choose All cities."

    dates = {}

    for key in ("start_date", "end_date"):
        raw = str(data.get(key) or "").strip()

        if not raw:
            dates[key] = None
            continue

        try:
            dates[key] = datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None, "Enter valid dates."

    if dates["start_date"] and dates["end_date"] \
            and dates["end_date"] < dates["start_date"]:
        return None, "End date cannot be before start date."

    return {
        "advertiser_name": advertiser,
        "title": title,
        "description": str(data.get("description") or "").strip()[:200],
        "image_url": image,
        "link_url": link,
        "placement": placement,
        "target_city": target_city or None,
        **dates
    }, None


@app.route("/api/admin/ads", methods=["GET", "POST"])
def admin_ads_api():

    if not is_admin_logged_in():
        return admin_denied()

    if request.method == "GET":
        try:
            rows = (
                supabase.table("ads")
                .select("*")
                .order("created_at", desc=True)
                .execute()
            ).data or []

            return jsonify({"success": True, "ads": rows})

        except Exception as error:
            print("Admin ads error:", str(error))
            return jsonify({
                "success": False,
                "message": "Unable to load ads."
            }), 500

    values, error = read_ad_form(request.get_json(silent=True) or {})

    if error:
        return jsonify({"success": False, "message": error}), 400

    try:
        supabase.table("ads").insert(values).execute()
        return jsonify({"success": True, "message": "Ad added."})

    except Exception as err:
        print("Admin add ad error:", str(err))
        return jsonify({
            "success": False,
            "message": "Unable to add the ad."
        }), 500


@app.route("/api/admin/ads/<int:ad_id>", methods=["PUT", "DELETE"])
def admin_edit_ad(ad_id):

    if not is_admin_logged_in():
        return admin_denied()

    try:
        if request.method == "DELETE":
            supabase.table("ads").delete().eq("id", ad_id).execute()
            return jsonify({"success": True, "message": "Ad deleted."})

        values, error = read_ad_form(request.get_json(silent=True) or {})

        if error:
            return jsonify({"success": False, "message": error}), 400

        result = (
            supabase.table("ads").update(values).eq("id", ad_id).execute()
        )

        if not result.data:
            return jsonify({
                "success": False,
                "message": "Ad not found."
            }), 404

        return jsonify({"success": True, "message": "Ad updated."})

    except Exception as err:
        print("Admin edit ad error:", str(err))
        return jsonify({
            "success": False,
            "message": "Unable to update the ad."
        }), 500


@app.route("/api/admin/ads/<int:ad_id>/toggle", methods=["POST"])
def admin_toggle_ad(ad_id):
    """Pause / resume an ad."""

    if not is_admin_logged_in():
        return admin_denied()

    try:
        rows = (
            supabase.table("ads")
            .select("is_active")
            .eq("id", ad_id)
            .execute()
        ).data

        if not rows:
            return jsonify({
                "success": False,
                "message": "Ad not found."
            }), 404

        supabase.table("ads").update(
            {"is_active": not rows[0]["is_active"]}
        ).eq("id", ad_id).execute()

        return jsonify({"success": True, "message": "Ad updated."})

    except Exception as err:
        print("Admin toggle ad error:", str(err))
        return jsonify({
            "success": False,
            "message": "Unable to update the ad."
        }), 500


@app.route("/admin/debug-ads")
def debug_ads():
    """Temporary: shows why ads are not showing. Delete after fixing."""

    if not is_admin_logged_in():
        return admin_denied()

    placement = request.args.get("placement", "emi")
    today = datetime.now(IST).date().isoformat()
    report = {"placement_checked": placement, "today_IST": today}

    try:
        rows = supabase.table("ads").select("*").execute().data or []
        report["total_ads_in_table"] = len(rows)
        report["ads"] = [
            {
                "id": r["id"],
                "title": r["title"],
                "placement": r["placement"],
                "is_active": r["is_active"],
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "matches_placement": r["placement"] == placement,
                "date_ok": not (r.get("start_date") and r["start_date"] > today)
                           and not (r.get("end_date") and r["end_date"] < today),
            }
            for r in rows
        ]
    except Exception as error:
        report["table_error"] = str(error)

    try:
        report["get_ads_returns"] = len(get_ads(placement))
    except Exception as error:
        report["get_ads_error"] = str(error)

    return jsonify(report)


@app.route("/admin/debug-reviews")
def debug_reviews():
    """Temporary: shows why reviews are not on the home page. Delete after fixing."""

    if not is_admin_logged_in():
        return admin_denied()

    report = {}

    try:
        rows = (
            supabase.table("purchase_ratings")
            .select("id, stars, status, public_consent")
            .execute()
        ).data or []

        by_status = {}
        for row in rows:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1

        report["total_reviews"] = len(rows)
        report["by_status"] = by_status
        report["approved_with_consent"] = sum(
            1 for r in rows if r["status"] == "approved" and r["public_consent"]
        )
    except Exception as error:
        report["table_error"] = str(error)

    try:
        shown, summary = load_public_testimonials()
        report["home_page_gets"] = len(shown)
        report["summary"] = summary
    except Exception as error:
        report["home_page_error"] = str(error)

    return jsonify(report)


# --------------------------------
# HEALTH CHECK
# --------------------------------

@app.route("/health")
def health():

    return jsonify({
        "status": "Zivo Flask server running",
        "supabase_configured": True
    })


# --------------------------------
# RUN APPLICATION
# --------------------------------

if __name__ == "__main__":

    print("Zivo server starting...")
    print("Environment file:", ENV_FILE)
    print("Supabase configured: True")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )