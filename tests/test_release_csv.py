import csv
import io

from conftest import add_monitor
from sqlalchemy import select

from apptrail.db import Observation, Run


def test_csv_preserves_search_identity_and_unranked_outcomes(client, tracked):
    monitors = [
        add_monitor(client, tracked, query="tasks", language="en", device="mobile", depth=1),
        add_monitor(client, tracked, query="tasks", language="fr", device="tablet", depth=2),
    ]
    for _ in monitors:
        assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        for obs, run in session.execute(select(Observation, Run).join(Run)):
            featured = run.monitor_id == monitors[0]
            obs.data = {
                "found": featured,
                "position": None,
                "section": "Featured" if featured else None,
                "mentioned": False,
                "cited": False,
            }
            obs.retrospective = featured
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export.csv").text)))
    assert len(rows) == 2
    by_monitor = {int(row["monitor_id"]): row for row in rows}
    featured, absent = (by_monitor[m] for m in monitors)
    assert featured["app_name"] == absent["app_name"] == "Todo Example"
    assert (featured["language"], featured["device"], featured["depth"]) == ("en", "mobile", "1")
    assert (absent["language"], absent["device"], absent["depth"]) == ("fr", "tablet", "2")
    assert featured["position"] == absent["position"] == ""
    assert (featured["found"], featured["section"], featured["retrospective"]) == (
        "True",
        "Featured",
        "True",
    )
    assert (absent["found"], absent["section"], absent["retrospective"]) == ("False", "", "False")


def test_csv_escapes_formula_names_and_keeps_citation_details(client, tracked):
    assert client.patch(f"/api/apps/{tracked}", json={"name": "=SUM(1,2)"}).status_code == 200
    add_monitor(client, tracked, source="google_ai_mode", query="@tasks")
    assert client.app.state.worker.process_one()
    with client.app.state.service.db.session.begin() as session:
        obs = session.scalar(select(Observation))
        obs.data = {**obs.data, "app_link": True, "website_cited": False, "cited": True}
    response = client.get("/api/export.csv")
    assert response.status_code == 200
    (row,) = csv.DictReader(io.StringIO(response.text))
    assert row["app_name"] == "'=SUM(1,2)"
    assert row["query"] == "'@tasks"
    assert (row["app_link"], row["website_cited"], row["cited"]) == ("True", "False", "True")
