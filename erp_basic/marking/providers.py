"""Контракт адаптера и детерминированный тестовый двойник; промышленных endpoints здесь нет."""

from typing import Protocol


class UncertainResult(Exception):
    """Запрос мог быть принят. Повтор отправки без сверки запрещён."""


class Provider(Protocol):
    def send(self, *, business_key: str, payload: dict, simulation: str) -> dict: ...
    def reconcile(self, *, business_key: str, payload: dict, simulation: str) -> dict: ...


class TestSimulator:
    """Не обращается к сети и не выдаёт документы/коды ГИС МТ или фискальные признаки."""

    def send(self, *, business_key, payload, simulation):
        if simulation == "unknown":
            raise UncertainResult("Тест: ответ потерян после условного принятия запроса.")
        return self._result(business_key, payload, simulation)

    def reconcile(self, *, business_key, payload, simulation):
        return self._result(business_key, payload, "accept" if simulation == "unknown" else simulation)

    @staticmethod
    def _result(business_key, payload, simulation):
        quantity = payload["quantity"]
        accepted = 0 if simulation == "reject" else (max(0, quantity - 1) if simulation == "partial" else quantity)
        return {"state": {"reject": "Rejected", "partial": "Partial"}.get(simulation, "Accepted"),
                "external_id": "SIM-" + business_key[:24], "accepted_count": accepted,
                "rejected_count": quantity - accepted, "test_only": True, "regulatory_confirmed": False,
                "fiscalized": False, "reason": "Результат тестового симулятора; внешней передачи не было."}


def provider_for(name, *, allow_tests, environment):
    if name != "test-simulator-v1" or not allow_tests or environment != "Test":
        raise ValueError("Подтверждённый промышленный адаптер не настроен; внешняя операция закрыта.")
    return TestSimulator()
