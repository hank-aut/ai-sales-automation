"""
Email safety checks — import this before sending ANY email.
Prevents placeholder text, missing names, and other embarrassments.

CRITICAL SF RULES:
- Before creating a Contact, ALWAYS check if a Lead exists for that email first.
- If a Lead exists, CONVERT the Lead to a Contact. Do NOT create a new Contact.
  Duplicate Leads/Contacts cause data integrity issues.
- To check: sf data query --query "SELECT Id FROM Lead WHERE Email = 'x@y.com'"
- To convert: use Lead Convert API or convert manually in SF.
- NEVER create a Contact when a Lead already exists for the same person.
"""

BLOCKED_PATTERNS = [
    "[Name]",
    "[name]",
    "[NAME]",
    "[First Name]",
    "[First name]",
    "[Company]",
    "[company]",
    "[COMPANY]",
    "[Title]",
    "[title]",
    "[Email]",
    "[email]",
    "{name}",
    "{company}",
    "{title}",
    "{{",
    "}}",
    "[Contact]",
    "[contact]",
    "[CONTACT]",
]

def validate_email_before_send(subject, body, to_email):
    """
    Validates an email before sending. Returns (ok, error_message).
    If ok is False, DO NOT SEND.
    """
    errors = []

    # Check for placeholder text in body
    for pattern in BLOCKED_PATTERNS:
        if pattern in body:
            errors.append(f"Body contains placeholder: '{pattern}'")
        if pattern in subject:
            errors.append(f"Subject contains placeholder: '{pattern}'")

    # Check that we have a real email address
    if not to_email or "@" not in to_email:
        errors.append(f"Invalid email address: '{to_email}'")

    # Check that body isn't empty
    if len(body.strip()) < 20:
        errors.append("Email body is too short")

    # Check that subject isn't empty
    if not subject or len(subject.strip()) < 3:
        errors.append("Subject is empty or too short")

    # Check body starts with a real name, not a placeholder
    body_stripped = body.strip()
    if body_stripped.startswith("[") or body_stripped.startswith("{"):
        errors.append("Body starts with a placeholder character")

    # Check for "info@" or generic emails
    if to_email and to_email.startswith("info@"):
        errors.append(f"Generic email address: '{to_email}'")

    if errors:
        return False, "EMAIL BLOCKED: " + "; ".join(errors)

    return True, "OK"


def validate_domain_match(email, company_domain):
    """Verify the enriched email's domain matches the target company's domain.
    Rejects personal domains and cross-company mismatches.
    Returns (ok, message).
    """
    if not email or "@" not in email:
        return False, "Invalid email address"

    email_domain = email.split("@", 1)[1].lower()

    personal = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "protonmail.com", "me.com", "mac.com"}
    if email_domain in personal:
        return False, f"Personal email domain: {email_domain}"

    if company_domain:
        d = company_domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").strip("/")
        if email_domain != d and d not in email_domain and email_domain.split(".")[0] != d.split(".")[0]:
            return False, f"Email domain {email_domain} does not match target {d}"

    return True, "OK"
