import csv
import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.mail import send_mail
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import CorrectionRequest, DailyTimeRecord, EmployeeProfile, HRReview, Notification


def is_hr(user):
    return user.groups.filter(name="HR").exists()


def _parse_browser_iso_to_local(iso_value):
    if not iso_value:
        return timezone.localtime(timezone.now())
    try:
        dt = datetime.datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return timezone.localtime(dt)
    except (ValueError, TypeError):
        return timezone.localtime(timezone.now())


def _get_active_record(profile):
    """Return the latest open record for this employee."""
    today = timezone.localdate()
    record = (
        DailyTimeRecord.objects.filter(
            employee=profile,
            date=today,
            clock_out__isnull=True,
        )
        .order_by("-clock_in")
        .first()
    )
    if record:
        return record
    # Fallback: any open record in case date differs due timezone/client clock mismatch.
    return (
        DailyTimeRecord.objects.filter(
            employee=profile,
            clock_out__isnull=True,
        )
        .order_by("-clock_in")
        .first()
    )


def _today_record(profile):
    """Get or create today's DailyTimeRecord for the given profile."""
    today = datetime.date.today()
    # Get the latest active (non-CLOCKED_OUT) record for today, or create new
    record = DailyTimeRecord.objects.filter(
        employee=profile,
        date=today,
        status__in=["WORKING", "ON_BREAK"],
    ).first()
    if record:
        return record
    # Create a new record for this shift
    record = DailyTimeRecord.objects.create(
        employee=profile,
        date=today,
        clock_in=timezone.now(),
        status="WORKING",
    )
    return record


def login_view(request):
    error = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if is_hr(user):
                return redirect("hr_dashboard")
            return redirect("punch_clock")
        error = "Ungültige Anmeldedaten."
    return render(request, "login.html", {"error": error})


def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def api_notifications(request):
    """Return unread notifications for the current user."""
    notifications_qs = Notification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).order_by("-created_at")
    notifications = []
    for notif in notifications_qs:
        payload = {
            "id": notif.id,
            "notification_type": notif.notification_type,
            "title": notif.title,
            "message": notif.message,
            "created_at": notif.created_at.isoformat(),
            "is_read": notif.is_read,
            "related_record_id": notif.related_record_id,
            "actions": [],
        }
        if notif.notification_type in ("EDIT_REQUEST", "CORRECTION") and notif.related_record_id:
            correction = (
                CorrectionRequest.objects.filter(record=notif.related_record, status="PENDING")
                .order_by("-id")
                .first()
            )
            if correction:
                payload["correction"] = {
                    "id": correction.id,
                    "record_date": correction.record.date.isoformat(),
                    "employee": str(correction.record.employee),
                    "proposed_clock_in": correction.proposed_clock_in.strftime("%H:%M")
                    if correction.proposed_clock_in else "",
                    "proposed_clock_out": correction.proposed_clock_out.strftime("%H:%M")
                    if correction.proposed_clock_out else "",
                    "proposed_break_minutes": correction.proposed_break_minutes,
                    "note": correction.note,
                    "is_delete_request": (
                        not correction.proposed_clock_in
                        and not correction.proposed_clock_out
                        and correction.proposed_break_minutes is None
                        and "Löschanfrage" in (correction.note or "")
                    ),
                }
                payload["actions"] = ["approve", "reject"]
        elif notif.notification_type == "REMINDER":
            payload["actions"] = ["acknowledge"]
        notifications.append(payload)
    return JsonResponse({
        "ok": True,
        "count": len(notifications),
        "notifications": notifications,
    })


@login_required
@require_POST
def api_mark_notification_read(request):
    """Mark a notification as read."""
    notif_id = request.POST.get("notification_id")
    try:
        notif = Notification.objects.get(pk=notif_id, recipient=request.user)
    except Notification.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Notification not found."})

    notif.is_read = True
    notif.save()
    return JsonResponse({"ok": True})


@login_required
@require_POST
def api_mark_all_notifications_read(request):
    """Mark all notifications as read for the current user."""
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    return JsonResponse({"ok": True})


@login_required
def punch_clock_view(request):
    profile = request.user.employeeprofile
    today = timezone.localdate()

    # Current active record (if any)
    record = _get_active_record(profile)

    # All completed records for today
    today_records = DailyTimeRecord.objects.filter(
        employee=profile,
        date=today,
        status="CLOCKED_OUT",
    ).order_by("clock_in")

    # Weekly overview: last 7 days (including today's completed records)
    week_start = today - datetime.timedelta(days=7)
    weekly_records = (
        DailyTimeRecord.objects.filter(employee=profile, date__gte=week_start, date__lte=today)
        .order_by("date", "clock_in")
    )

    month_records = DailyTimeRecord.objects.filter(
        employee=profile,
        date__month=today.month,
        date__year=today.year,
    )
    actual_monthly = Decimal("0.0")
    for rec in month_records:
        if rec.net_hours is not None:
            actual_monthly += Decimal(str(rec.net_hours))
    planned_monthly = profile.target_hours_per_month
    monthly_delta = actual_monthly - planned_monthly

    # Notification count
    unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()

    ctx = {
        "profile": profile,
        "record": record,
        "today_records": today_records,
        "weekly_records": weekly_records,
        "planned_monthly": round(planned_monthly, 2),
        "actual_monthly": round(actual_monthly, 2),
        "monthly_delta": round(monthly_delta, 2),
        "unread_count": unread_count,
    }
    return render(request, "punch_clock.html", ctx)


@login_required
@require_POST
def api_punch_in(request):
    profile = request.user.employeeprofile
    browser_clock_in = request.POST.get("clock_in")
    clock_in_local = _parse_browser_iso_to_local(browser_clock_in)
    today = clock_in_local.date()

    # Check for any active (non-clocked-out) record today
    active = _get_active_record(profile)

    if active:
        return JsonResponse({"ok": False, "error": "Already clocked in. Please clock out first."})

    record = DailyTimeRecord.objects.create(
        employee=profile,
        date=today,
        clock_in=clock_in_local,
        status="WORKING",
    )

    return JsonResponse({
        "ok": True,
        "status": record.status,
        "clock_in": record.clock_in.strftime("%H:%M"),
        "clock_in_iso": timezone.localtime(record.clock_in).isoformat(),
    })


@login_required
@require_POST
def api_break_start(request):
    profile = request.user.employeeprofile
    record = _get_active_record(profile)

    if not record or record.status != "WORKING":
        return JsonResponse({"ok": False, "error": "Not currently working."})

    record.break_start = timezone.now()
    record.status = "ON_BREAK"
    record.save()
    return JsonResponse({"ok": True, "status": record.status})


@login_required
@require_POST
def api_break_end(request):
    profile = request.user.employeeprofile
    record = _get_active_record(profile)

    if not record or record.status != "ON_BREAK":
        return JsonResponse({"ok": False, "error": "Not currently on break."})

    if record.break_start:
        delta = (timezone.now() - record.break_start).total_seconds()
        record.total_break_minutes += int(delta // 60)
    record.break_start = None
    record.status = "WORKING"
    record.save()
    return JsonResponse({"ok": True, "status": record.status, "break_min": record.total_break_minutes})


@login_required
@require_POST
def api_punch_out(request):
    user = request.user
    try:
        profile = user.employeeprofile
    except EmployeeProfile.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Employee profile not found."})
    record = _get_active_record(profile)

    if not record:
        return JsonResponse({"ok": False, "error": "No active record found to clock out."})

    # End any running break
    if record.status == "ON_BREAK" and record.break_start:
        delta = (timezone.now() - record.break_start).total_seconds()
        record.total_break_minutes += int(delta // 60)
        record.break_start = None

    record.clock_out = timezone.localtime(timezone.now())
    if record.clock_in and record.clock_out < record.clock_in:
        record.clock_out = record.clock_in
    record.status = "CLOCKED_OUT"
    record.save(update_fields=["break_start", "total_break_minutes", "clock_out", "status"])

    return JsonResponse({
        "ok": True,
        "status": record.status,
        "clock_in": record.clock_in.strftime("%H:%M") if record.clock_in else "",
        "clock_out": record.clock_out.strftime("%H:%M"),
        "net_hours": record.net_hours,
        "break_min": record.total_break_minutes,
    })


@login_required
@user_passes_test(is_hr, login_url="/access-denied/")
@require_POST
def api_approve_correction(request):
    notification_id = request.POST.get("notification_id")
    decision = request.POST.get("decision", "APPROVE").upper()
    if decision not in ("APPROVE", "REJECT"):
        return JsonResponse({"ok": False, "error": "Invalid decision."})

    try:
        notif = Notification.objects.get(pk=notification_id, recipient=request.user, is_read=False)
    except Notification.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Notification not found."})

    if notif.notification_type not in ("EDIT_REQUEST", "CORRECTION"):
        return JsonResponse({"ok": False, "error": "Not a correction notification."})

    correction = (
        CorrectionRequest.objects.filter(record=notif.related_record, status="PENDING")
        .order_by("-id")
        .first()
    )
    if not correction:
        notif.is_read = True
        notif.save(update_fields=["is_read"])
        return JsonResponse({"ok": False, "error": "No pending correction found for this notification."})

    record = correction.record
    employee_user = record.employee.user
    is_delete_request = (
        not correction.proposed_clock_in
        and not correction.proposed_clock_out
        and correction.proposed_break_minutes is None
        and "Löschanfrage" in (correction.note or "")
    )
    if decision == "APPROVE":
        if is_delete_request:
            record.delete()
        else:
            if correction.proposed_clock_in:
                tz = timezone.get_current_timezone()
                record.clock_in = timezone.make_aware(
                    datetime.datetime.combine(record.date, correction.proposed_clock_in), tz
                )
            if correction.proposed_clock_out:
                tz = timezone.get_current_timezone()
                record.clock_out = timezone.make_aware(
                    datetime.datetime.combine(record.date, correction.proposed_clock_out), tz
                )
            if correction.proposed_break_minutes is not None:
                record.total_break_minutes = correction.proposed_break_minutes
            if record.clock_out:
                record.status = "CLOCKED_OUT"
            record.save()
        correction.status = "APPROVED"
        correction.save(update_fields=["status"])
        if is_delete_request:
            employee_title = "Löschanfrage genehmigt"
            employee_msg = "Ihre Löschanfrage wurde genehmigt. Der Eintrag wurde entfernt."
        else:
            employee_title = "Korrektur genehmigt"
            employee_msg = (
                f"Ihre Anfrage für {record.date} wurde genehmigt. "
                f"Eintrag wurde aktualisiert."
            )
    else:
        correction.status = "REJECTED"
        correction.save(update_fields=["status"])
        employee_title = "Korrektur abgelehnt"
        employee_msg = (
            f"Ihre Anfrage für {record.date} wurde abgelehnt. "
            f"Bitte prüfen Sie Ihre Angaben und senden Sie ggf. eine neue Anfrage."
        )

    notif.is_read = True
    notif.save(update_fields=["is_read"])
    Notification.objects.create(
        recipient=employee_user,
        sender=request.user,
        notification_type="INFO",
        title=employee_title,
        message=employee_msg,
        related_record=None if is_delete_request else record,
    )
    return JsonResponse({"ok": True, "decision": decision, "notification_id": notif.id})


@login_required
@require_POST
def api_submit_correction(request):
    """Submit a correction/edit request for a time record."""
    profile = request.user.employeeprofile
    record_id = request.POST.get("record_id")
    proposed_clock_in = request.POST.get("proposed_clock_in", "").strip()
    proposed_clock_out = request.POST.get("proposed_clock_out", "").strip()
    proposed_break = request.POST.get("proposed_break_minutes", "").strip()
    note = request.POST.get("note", "").strip()

    try:
        record = DailyTimeRecord.objects.get(pk=record_id, employee=profile)
    except DailyTimeRecord.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Record not found."})

    correction_data = {"note": note}

    if proposed_clock_in:
        try:
            hour, minute = proposed_clock_in.split(":")
            correction_data["proposed_clock_in"] = datetime.time(int(hour), int(minute))
        except (ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "Invalid clock-in time format."})

    if proposed_clock_out:
        try:
            hour, minute = proposed_clock_out.split(":")
            correction_data["proposed_clock_out"] = datetime.time(int(hour), int(minute))
        except (ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "Invalid clock-out time format."})

    if proposed_break:
        try:
            correction_data["proposed_break_minutes"] = int(proposed_break)
        except (ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "Invalid break minutes."})

    correction = CorrectionRequest.objects.create(record=record, **correction_data)

    # Send notification to all HR users
    from django.contrib.auth.models import User
    hr_users = User.objects.filter(groups__name="HR")
    for hr_user in hr_users:
        Notification.objects.create(
            recipient=hr_user,
            sender=request.user,
            notification_type="EDIT_REQUEST",
            title=f"Bearbeitungsanfrage von {profile}",
            message=f"{profile} hat eine Korrektur für {record.date} angefragt. {note}",
            related_record=record,
        )

    return JsonResponse({"ok": True})


@login_required
@require_POST
def api_delete_record(request):
    """Submit deletion request for HR approval (does not delete immediately)."""
    profile = request.user.employeeprofile
    record_id = request.POST.get("record_id")

    try:
        record = DailyTimeRecord.objects.get(pk=record_id, employee=profile)
    except DailyTimeRecord.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Record not found."})

    if record.status not in ("CLOCKED_OUT", "MISSING_CLOCKOUT"):
        return JsonResponse({"ok": False, "error": "Cannot delete an active record."})

    correction = CorrectionRequest.objects.create(
        record=record,
        note=f"Löschanfrage für Eintrag am {record.date} von {profile}.",
    )
    from django.contrib.auth.models import User
    hr_users = User.objects.filter(groups__name="HR")
    for hr_user in hr_users:
        Notification.objects.create(
            recipient=hr_user,
            sender=request.user,
            notification_type="EDIT_REQUEST",
            title=f"Löschanfrage von {profile}",
            message=f"{profile} möchte den Zeiteintrag vom {record.date} löschen.",
            related_record=record,
        )
    return JsonResponse({"ok": True, "request_id": correction.id})


@login_required
@user_passes_test(is_hr, login_url="/access-denied/")
def hr_dashboard_view(request):
    today = datetime.date.today()

    # Allow month/year selection, default to current month
    try:
        selected_month = int(request.GET.get("month", today.month))
        selected_year = int(request.GET.get("year", today.year))
    except (ValueError, TypeError):
        selected_month = today.month
        selected_year = today.year

    # Filter out HR group members from the employee list
    employees = EmployeeProfile.objects.select_related("user").exclude(
        user__groups__name="HR"
    ).all()

    employee_data = []

    for emp in employees:
        records = DailyTimeRecord.objects.filter(
            employee=emp,
            date__month=selected_month,
            date__year=selected_year,
        ).order_by("date", "clock_in")

        actual_hours = Decimal("0.0")
        for r in records:
            if r.net_hours is not None:
                actual_hours += Decimal(str(r.net_hours))

        target = emp.target_hours_per_month
        delta = actual_hours - target

        # HR review status
        hr_review, _ = HRReview.objects.get_or_create(
            employee=emp,
            month=selected_month,
            year=selected_year,
            defaults={"status": "PENDING"},
        )

        employee_data.append({
            "profile": emp,
            "target": target,
            "actual": round(actual_hours, 2),
            "delta": round(delta, 2),
            "records": records,
            "hr_review": hr_review,
        })

    # Notification count for HR
    unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()

    # Month choices for the selector
    month_choices = [
        (1, "Jan"), (2, "Feb"), (3, "Mär"), (4, "Apr"),
        (5, "Mai"), (6, "Jun"), (7, "Jul"), (8, "Aug"),
        (9, "Sep"), (10, "Okt"), (11, "Nov"), (12, "Dez"),
    ]

    ctx = {
        "employee_data": employee_data,
        "current_month": selected_month,
        "current_year": selected_year,
        "unread_count": unread_count,
        "month_choices": month_choices,
    }
    return render(request, "hr_dashboard.html", ctx)


def access_denied_view(request):
    return render(request, "access_denied.html", status=403)


@login_required
@user_passes_test(is_hr, login_url="/access-denied/")
@require_POST
def api_send_reminder(request):
    review_id = request.POST.get("review_id")
    message = request.POST.get("message", "").strip()
    try:
        review = HRReview.objects.get(pk=review_id)
    except HRReview.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Review not found."})

    subject = f"Erinnerung Zeiterfassung {review.month}/{review.year}"
    default_msg = (
        f"Bitte überprüfen und korrigieren Sie Ihre Zeiteinträge für "
        f"{review.month}/{review.year}."
    )
    mail_body = message if message else default_msg
    recipient_email = (review.employee.user.email or "").strip()
    email_sent = False
    email_error = ""
    if recipient_email:
        try:
            send_mail(
                subject,
                mail_body,
                getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@fitlife.local"),
                [recipient_email],
                fail_silently=False,
            )
            email_sent = True
        except Exception:
            email_error = "E-Mail Versand fehlgeschlagen. In-App Erinnerung wurde trotzdem zugestellt."
    else:
        email_error = "Keine E-Mail im Profil. In-App Erinnerung wurde zugestellt."

    review.status = "REMINDER_SENT"
    review.save()

    # Create in-app notification for the employee
    Notification.objects.create(
        recipient=review.employee.user,
        sender=request.user,
        notification_type="REMINDER",
        title=f"Erinnerung von HR",
        message=mail_body,
    )

    return JsonResponse({
        "ok": True,
        "status": review.status,
        "email_sent": email_sent,
        "warning": email_error,
    })


@login_required
@user_passes_test(is_hr, login_url="/access-denied/")
@require_POST
def api_acknowledge(request):
    review_id = request.POST.get("review_id")
    try:
        review = HRReview.objects.get(pk=review_id)
    except HRReview.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Review not found."})

    review.status = "REVIEWED"
    review.save()
    return JsonResponse({"ok": True, "status": review.status})


@login_required
@user_passes_test(is_hr, login_url="/access-denied/")
def csv_export_view(request):
    today = datetime.date.today()

    try:
        selected_month = int(request.GET.get("month", today.month))
        selected_year = int(request.GET.get("year", today.year))
    except (ValueError, TypeError):
        selected_month = today.month
        selected_year = today.year

    records = (
        DailyTimeRecord.objects.filter(date__month=selected_month, date__year=selected_year)
        .select_related("employee__user")
        .exclude(employee__user__groups__name="HR")
        .order_by("employee__user__last_name", "date", "clock_in")
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="time_report_{selected_year}_{selected_month:02d}.csv"'
    )

    writer = csv.writer(response)
    writer.writerow(["Employee", "Date", "Clock-in", "Clock-out", "Break(min)", "Net Hours"])

    for r in records:
        writer.writerow([
            str(r.employee),
            r.date.isoformat(),
            r.clock_in.strftime("%H:%M") if r.clock_in else "",
            r.clock_out.strftime("%H:%M") if r.clock_out else "",
            r.total_break_minutes,
            r.net_hours if r.net_hours is not None else "",
        ])

    return response
