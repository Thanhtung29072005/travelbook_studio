"""Inject a persistent Review error into the fixture workflow; prove bounded repair/PDF."""

import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.revisions import REPAIR_PLANS
from backend.app.schemas import Fact, Review, ReviewIssue
from backend.app.workflow import TravelBookWorkflow

ERRORS = {"image_curator": ("photo", "LLM_PHOTO_LOCATION"), "illustrator": ("style", "LLM_STYLE_CONSISTENCY"),
    "planner": ("itinerary", "OPENING_WINDOW"), "researcher": ("research", "INPUT_UNVERIFIED"),
    "budget": ("budget", "BUDGET_LIMIT"), "content_editor": ("content", "CONTENT_QUALITY"),
    "map_agent": ("map", "MAP_ROUTE"), "layout_editor": ("layout", "LAYOUT_OVERFLOW"), "pm": ("itinerary", "SCHEMA_TRIP_REQUEST")}


class InjectedWorkflow(TravelBookWorkflow):
    def __init__(self, *args, injected_agent="image_curator", clear_after=None, **kwargs):
        self.injected_agent, self.clear_after = injected_agent, clear_after
        super().__init__(*args, **kwargs)

    def _review(self, state, loaded):
        result = super()._review(state, loaded)
        count = state.get("revision_count", 0)
        if self.clear_after is None or count < self.clear_after:
            category, code = ERRORS[self.injected_agent]
            result["review"] = Review(verdict="REVISE", source="test:injected-review", confidence=1, revision=count,
                summary=Fact[str](value="Intentional persistent error to test routing", source="test:injected-review", confidence=1),
                issues=[ReviewIssue(category=category, target_agent=self.injected_agent, severity="major", rule_code=code,
                    message=Fact[str](value="Injected error; this is a routing test, not a real quality finding", source="test:injected-review", confidence=1))])
            result["artifacts"] = [self._save_json(state, f"review_reports/injected_r{count}.json", result["review"], "reviewer", "review",
                source="test:injected-review", confidence=1)]
        return result


def main(args):
    settings = Settings()
    if args.max_revisions is not None:
        settings = Settings.model_validate({**settings.model_dump(), "max_revisions": args.max_revisions})
    if args.clear_after is not None and args.clear_after < 0:
        raise ValueError("clear-after must be nonnegative")
    db = Database(settings.database_path)
    workflow = InjectedWorkflow(db, settings=settings, artifacts_root=settings.artifacts_dir,
        injected_agent=args.agent, clear_after=args.clear_after)
    result = workflow.run(project_id="revision-routing-"+uuid4().hex[:12])
    events = db.get_events(result["project_id"])
    requests = [event["payload"] for event in events if event["event_type"] == "revision_requested"]
    repaired = [event["payload"]["agent"] for event in events if event["event_type"] == "repair_completed"]
    expected_count = min(args.clear_after, settings.max_revisions) if args.clear_after is not None else settings.max_revisions
    expected_exhaustion = args.clear_after is None or args.clear_after > settings.max_revisions
    if result["revision_count"] != expected_count or result["revision_exhausted"] != expected_exhaustion:
        raise AssertionError("Wrong repair limit or exhaustion status")
    if repaired != REPAIR_PLANS[args.agent] * expected_count or any(item["target_agent"] != args.agent for item in requests):
        raise AssertionError("Repair graph went to the wrong agent or missed dependencies")
    if not Path(result["final_pdf"].path).read_bytes().startswith(b"%PDF-"):
        raise AssertionError("Graph did not continue to PDF")
    manifest = json.loads(Path(result["final_pdf"].path).with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if bool(manifest["revision_warnings"]) != expected_exhaustion:
        raise AssertionError("PDF warning does not reflect exhaustion")
    report = {"project_id": result["project_id"], "mode": "OFFLINE FIXTURE; INTENTIONAL REVIEW ERROR",
        "injected_agent": args.agent, "max_revisions": settings.max_revisions, "revision_count": result["revision_count"],
        "review_count": sum(event["event_type"] == "fixture_review" for event in events), "repair_agents": repaired,
        "revision_exhausted": result["revision_exhausted"], "warning_events": sum(event["event_type"] == "revision_limit_reached" for event in events),
        "final_verdict": result["review"].verdict, "completed": result["completed"], "production_approved": result["production_approved"],
        "pdf": result["final_pdf"].path, "warnings_in_pdf_manifest": bool(manifest["revision_warnings"])}
    output = settings.artifacts_dir / result["project_id"] / "revision_validation.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=sorted(ERRORS), default="image_curator")
    parser.add_argument("--max-revisions", type=int)
    parser.add_argument("--clear-after", type=int)
    main(parser.parse_args())
