import bcrypt
import secrets

import peewee
from quart import session, redirect, url_for, Blueprint, render_template, request, abort, flash
from quart import current_app as app
from dataclasses import dataclass
from quart_schema.validation import DataSource
from quart_schema import QuartSchema, validate_request, validate_response
from email_validator import validate_email, EmailNotValidError

import settings
from funding import login_required, admin_required, moderator_required
from funding.factory import openid, database
from funding.auth.models import UserRegisterForm
from funding.models.database import User, PasswordReset, UserRole
from funding.utils.mail import send_mail, mail_enabled


bp_auth = Blueprint('bp_auth', __name__)


async def rotate_session():
    # quart-session 1.0.4 has no regenerate API. Delete before authenticating.
    interface = app.session_interface
    await interface.delete(key=interface.key_prefix + session.sid,
                           app=app._get_current_object())
    session.clear()
    session.sid = secrets.token_urlsafe(32)
    session.permanent = interface.permanent
    # The stale-password hook may have cleared the original session metadata.
    session.addr = (request.headers.get('X-Forwarded-For')
                    if app.config.get('SESSION_REVERSE_PROXY', False)
                    else request.remote_addr)


@bp_auth.route("/auth/login/", methods=['GET', 'POST'])
async def login():
    if settings.OPENID_CFG:
        if request.method == "POST":
            raise Exception("GET only")
        return redirect(url_for(openid.endpoint_name_login))
    elif request.method == "POST":
        blob = await request.form
        username = blob.get('username')
        password = blob.get('password')

        try:
            if not username or not password:
                raise Exception("No credentials")
            user = User.validate(username, password)
            if not user.enabled:
                await flash("user is disabled")
                return await render_template("login.html")
            await rotate_session()
            session['user'] = await user.to_json()
            await flash("Successful log-in")
            return redirect(url_for('bp_routes.root'))
        except Exception as ex:
            await flash("login failed")
            return await render_template("login.html")
    return await render_template('login.html')


RESET_SUBJECT = "Password reset for {domain}"

RESET_BODY = """Hello {username},

A password reset was requested for your account on {domain}.

Open this link to choose a new password:

{link}

The link is valid for {minutes} minutes and can be used only once.

If you did not request this, you can ignore this mail and your password
stays unchanged.
"""

RESET_SENT_MESSAGE = ("If that email address belongs to an account, a reset "
                      "link is on its way. Check your spam folder if it does "
                      "not arrive.")


def reset_link(token: str) -> str:
    scheme = getattr(settings, "URL_SCHEME", "https")
    path = url_for('bp_auth.reset', token=token, _external=False)
    return f"{scheme}://{settings.DOMAIN}{path}"


async def send_reset_mail(email: str):
    user = None
    try:
        user = User.select().where(User.mail == email).get()
    except peewee.DoesNotExist:
        pass

    if user and user.enabled and not user.oip:
        token = PasswordReset.issue(user)
        if token:
            try:
                await send_mail(
                    user.mail,
                    RESET_SUBJECT.format(domain=settings.DOMAIN),
                    RESET_BODY.format(
                        username=user.username,
                        domain=settings.DOMAIN,
                        link=reset_link(token),
                        minutes=int(PasswordReset.ttl() / 60)
                    )
                )
            except Exception as ex:
                PasswordReset.revoke(token)
                app.logger.error(f"PASSWORD_RESET_MAIL_FAILED: {ex}")


@bp_auth.route("/auth/forgot/", methods=['GET', 'POST'])
async def forgot():
    if settings.OPENID_CFG:
        return abort(404)

    if not mail_enabled():
        message = ("Self-service password resets are not available. Please ask "
                   "an admin to set a new password on your user page.")
        return await render_template("error.html", message=message, code=503), 503

    if request.method == "GET":
        return await render_template("forgot.html")

    blob = await request.form
    email = (blob.get('email') or '').strip()
    captcha = blob.get('captcha')

    if not captcha or captcha != session.get('captcha'):
        await flash("Invalid captcha!")
        return await render_template("forgot.html", email=email)

    session.pop('captcha', None)

    try:
        validated = validate_email(email, check_deliverability=False)
        email = getattr(validated, 'normalized', None) or validated.email
    except EmailNotValidError:
        await flash(RESET_SENT_MESSAGE)
        return await render_template("forgot.html")

    app.add_background_task(send_reset_mail, email)
    await flash(RESET_SENT_MESSAGE)
    return await render_template("forgot.html")


@bp_auth.route("/auth/reset/<token>", methods=['GET', 'POST'])
async def reset(token: str):
    if settings.OPENID_CFG:
        return abort(404)

    if not mail_enabled():
        return abort(404)

    expired = ("This password reset link is invalid, expired or already used. "
               "Please request a new one.")

    if not PasswordReset.resolve(token):
        return await render_template("error.html", message=expired, code=404), 404

    if request.method == "GET":
        return await render_template("reset.html", token=token)

    blob = await request.form
    password = blob.get('password', '')
    confirm = blob.get('password_confirm', '')

    if len(password) <= 5:
        await flash("Password length must exceed 5 characters")
        return await render_template("reset.html", token=token)

    if password != confirm:
        await flash("Passwords do not match")
        return await render_template("reset.html", token=token)

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    with database.atomic():
        user = PasswordReset.consume(token)
        if user:
            user.password = password_hash
            user.save(only=[User.password])

    if not user:
        return await render_template("error.html", message=expired, code=404), 404

    session.clear()
    await flash("Password changed. Please log in.")
    return redirect(url_for('bp_auth.login'))


@bp_auth.route("/auth/user/<path:name>/password", methods=["POST"])
@admin_required
async def user_password_set(name: str):
    try:
        user = User.select().filter(User.username == name).get()
    except:
        return abort(404)

    blob = await request.form
    csrf_token = blob.get('csrf_token', '')
    if not csrf_token or not secrets.compare_digest(
            csrf_token.encode(), session.get('csrf_token', '').encode()):
        return abort(403)
    password = blob.get('password', '')

    if len(password) <= 5:
        await flash("Password length must exceed 5 characters")
        return redirect(url_for('bp_routes.user_page', name=name))

    user.password = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with database.atomic():
        user.save(only=[User.password])
        PasswordReset.revoke_user(user.uuid)

    await flash(f"Password set for user '{user.username}'")
    return redirect(url_for('bp_routes.user_page', name=name))


@bp_auth.route("/auth/user/<path:name>/admin/toggle", methods=["POST"])
@moderator_required
async def user_admin_toggle(name: str):
    try:
        user = User.select().filter(User.username == name).get()
    except:
        return abort(404)

    if user.role == UserRole.admin:
        user.role = UserRole.user
    else:
        user.role = UserRole.admin
    user.save()

    await flash(f"User is admin: {user.role == UserRole.admin}")
    return redirect(url_for('bp_routes.user_page', name=name))


@bp_auth.route("/auth/user/<path:name>/moderator/toggle", methods=["POST"])
@moderator_required
async def user_moderator_toggle(name: str):
    try:
        user = User.select().filter(User.username == name).get()
    except:
        return abort(404)

    if user.role == UserRole.moderator:
        user.role = UserRole.user
    else:
        user.role = UserRole.moderator
    user.save()

    await flash(f"User is moderator: {user.role == UserRole.moderator}")
    return redirect(url_for('bp_routes.user_page', name=name))


@bp_auth.route("/auth/user/<path:name>/enabled/toggle", methods=["POST"])
@moderator_required
async def user_enabled_toggle(name: str):
    try:
        user = User.select().filter(User.username == name).get()
    except:
        return abort(404)

    if user.is_admin:
        return "cannot enable/disable an admin user"

    user.enabled = not user.enabled
    user.save()

    if user.enabled:
        msg = f"user '{user.username}' has been unbanned"
    else:
        msg = f"user '{user.username}' has been banned"

    await flash(msg)
    return redirect(url_for('bp_routes.user_page', name=name))


@bp_auth.get("/auth/register/")
async def register():
    return await render_template("register.html")


@bp_auth.post("/auth/register/")
@validate_request(UserRegisterForm, source=DataSource.FORM)
async def register_post(data: UserRegisterForm):
    if data.captcha != session['captcha']:
        await flash("Invalid captcha!")
        return await render_template('register.html', username=data.username, password=data.password, email=data.email)

    if len(data.password) <= 5:
        await flash("Password length must exceed 5 characters")
        return await render_template('register.html', username=data.username, password=data.password, email=data.email)

    try:
        User.select().where(User.username == data.username).get()
        await flash("Username taken.")
        return await render_template('register.html', username=data.username, password=data.password, email=data.email)
    except peewee.DoesNotExist:
        pass

    user_count = User.select().count()
    hashed = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt()).decode()
    user = User.create(
        username=data.username,
        password=hashed,
        mail=data.email,
        role=UserRole.admin if user_count == 0 else UserRole.user
    )

    await rotate_session()
    session['user'] = await user.to_json()
    await flash(f"Welcome {user.username}!")
    return redirect(url_for('bp_routes.root'))


@bp_auth.route("/logout")
@login_required
async def logout():
    session['user'] = None
    return redirect(url_for('bp_routes.root'))
