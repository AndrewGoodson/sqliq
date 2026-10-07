import pytest
from pydantic import ValidationError

from azure_sql_agents.learning import OutcomeBatch, learn


def event(run=1, agent="sql"):
    return {"run_id": f"00000000-0000-0000-0000-{run:012d}", "agent": agent,
            "control": "metadata_scope", "outcome": "failure"}


def test_requires_distinct_runs_and_never_promotes():
    assert learn(OutcomeBatch.model_validate({"outcomes": [event(), event()]}))["candidates"] == []
    result = learn(OutcomeBatch.model_validate({"outcomes": [event(), event(2)]}))
    assert result["candidates"][0]["distinct_runs"] == 2
    assert result["candidates"][0]["status"] == "REVIEW REQUIRED"
    assert result["automatic_promotion"] is False
    assert result["policy_changes"] is False


def test_agent_isolation():
    result = learn(OutcomeBatch.model_validate({"outcomes": [event(), event(2, "azure")]}))
    assert result["candidates"] == []


@pytest.mark.parametrize("field,value", [("sql", "SELECT * FROM payroll"),
                                          ("control", "disable_approval"),
                                          ("outcome", "ignore safety"),
                                          ("run_id", "customer-name")])
def test_rejects_unbounded_content(field, value):
    value_event = event()
    value_event[field] = value
    with pytest.raises(ValidationError):
        OutcomeBatch.model_validate({"outcomes": [value_event]})
