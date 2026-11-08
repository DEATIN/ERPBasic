"""Домен маркировки: проверка синтаксиса и явные учебные политики.

Модуль не обращается в ГИС МТ, не подтверждает подлинность кода и не разрешает
регуляторные операции. Его можно проверять без установленного Frappe.
"""

from .codes import CodeError, ParsedCode, parse_code
from .policy import PolicyError, get_profile, list_profiles, readiness

__all__ = [
    "CodeError",
    "ParsedCode",
    "PolicyError",
    "get_profile",
    "list_profiles",
    "parse_code",
    "readiness",
]
