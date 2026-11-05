"""Small reusable validators; no storage, network access or implicit state changes."""
import re


class InputError(ValueError):
    pass


def is_cancel(text):
    value = (text or '').strip().lower()
    return value in ('/cancel', 'cancel') or bool(re.fullmatch(r'/cancel@[a-z0-9_]+', value))


def quantity(text, maximum=999999):
    value = (text or '').strip()
    if not re.fullmatch(r'[0-9]{1,9}', value):
        raise InputError('Send a whole number, for example 1 or 3.')
    number = int(value)
    if not 1 <= number <= maximum:
        raise InputError(f'Choose a quantity from 1 to {maximum}.')
    return number


def text_value(value, maximum=3000, minimum=1):
    if not isinstance(value, str):
        raise InputError('Please send text for this field.')
    value = (value or '').strip()
    if not minimum <= len(value) <= maximum:
        raise InputError(f'Send text containing {minimum}–{maximum} characters.')
    return value


def email(value):
    value = text_value(value, 254)
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value):
        raise InputError('Please send a valid email address.')
    local, domain = value.rsplit('@', 1)
    if local.startswith('.') or local.endswith('.') or '..' in local or any(
            part.startswith('-') or part.endswith('-') or len(part) > 63 for part in domain.split('.')):
        raise InputError('Please send a valid email address.')
    return local + '@' + domain.lower()


def phone(value):
    value = (value or '').strip()
    if not re.fullmatch(r'\+?[0-9 ()\-]{7,40}', value) or not 7 <= sum(c.isdigit() for c in value) <= 15:
        raise InputError('Please send a valid phone/contact number.')
    return value


def field_value(message, kind):
    if kind == 'photo':
        if not message.photo:
            raise InputError('Please upload a photo for this field.')
        return {'kind': 'photo', 'file_id': message.photo[-1].file_id}
    if kind == 'contact':
        if not message.contact:
            raise InputError('Please share a Telegram contact using the attachment menu.')
        return {'kind': 'contact', 'phone': phone(message.contact.phone_number),
                'first_name': message.contact.first_name, 'user_id': message.contact.user_id}
    validators = {'email': email, 'phone': phone, 'text': text_value}
    if kind not in validators:
        raise InputError('This field type is not supported. Contact support.')
    return {'kind': 'text', 'value': validators[kind](message.text)}
