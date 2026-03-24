from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SQUADRON_POOL = [
    "Phoenix", "Reaper", "Ghost", "Viper", "Iceman",
    "Maverick", "Shadow", "Thunder", "Raptor", "Falcon",
    "Specter", "Warden", "Nomad", "Corsair", "Sentinel",
]

PILOT_TRAITS = [
    "meticulous", "cocky", "methodical", "terse", "eager",
    "grizzled", "cautious", "scrappy", "perfectionist", "laid-back",
]

# --- Quote Pools ---
# Randomized per-launch for splash screens

MINI_BOSS_QUOTES = [
    # Orchestrator / command & control flavor
    ("All stations, this is actual. Commence operations.", "CIC Watch Officer"),
    ("Set condition one throughout the ship.", "Battlestar Galactica, Adama"),
    ("I love it when a plan compiles.", "Hannibal, The A-Deploy"),
    ("In the pipe, five by five.", "Ferro, Aliens"),
    ("All ahead full. Rig ship for ultra-quiet.", "Captain Ramius"),
    ("Execute Order 66... tickets.", "The Emperor of Backlogs"),
    ("The spice must flow. The PRs must merge.", "Dune: Deployment Edition"),
    ("This is where the fun begins.", "Anakin, pre-regression"),
    ("Stay on target... stay on target...", "Gold Leader, Sprint Planning"),
    ("I have the conn. All departments report status.", "Officer of the Deck"),
    ("Weapons free. All callsigns cleared hot.", "AWACS Marshal"),
    ("Deploy the fleet. Every last ship.", "Admiral Ackbar, pre-merge"),
    ("War is a series of catastrophes that results in a victory.", "Clemenceau, on sprint planning"),
    ("No plan survives contact with the codebase.", "Helmuth von Moltke, DevOps"),
    ("The board is set. The pieces are moving.", "Gandalf, standup meeting"),
    ("Orchestration is the art of making others productive.", "Mini Boss Field Manual"),
    ("Multiple contacts bearing zero-nine-zero. Assigning intercepts.", "CIC Tactical"),
    ("We are the watchers on the wall. And also the deployers.", "Night Boss"),
    ("All birds in the air. Flight deck is clear.", "Air Boss"),
    ("Condition green across all stations. Steady as she goes.", "OOD"),
]

PILOT_LAUNCH_QUOTES = [
    # Classic aviation / fighter pilot
    ("I feel the need... the need for speed.", "Maverick and Goose"),
    ("You can be my wingman any time.", "Iceman"),
    ("Speed is life. Altitude is life insurance.", "Aviation Proverb"),
    ("Fly the airplane first. Debug second.", "USS Tenkara SOP"),
    ("Check six. Clear. Engaging.", "Standard Brevity"),
    ("Fox three. Commits away.", "Weapons Hot"),
    ("Boards are green. Ready for cat shot.", "Launch Officer"),
    ("Contact. Tally one. Engaging.", "AWACS Brevity"),
    ("Good tone. Good tone. Fox two.", "Weapons Officer"),
    ("Gear up, flaps up, brain on.", "Pre-Takeoff Checklist"),
    ("Turn and burn.", "Every Pilot Ever"),
    # Chuck Yeager / test pilot
    ("You concentrate on results. No risk is too great to prevent the job from getting done.", "Chuck Yeager"),
    ("There is no such thing as a natural born pilot.", "Chuck Yeager"),
    ("Rules are made for people who are not willing to make up their own.", "Chuck Yeager"),
    ("Just before you break through the sound barrier the cockpit shakes the most.", "Chuck Yeager"),
    # Real fighter pilot wisdom
    ("Go in close and when you think you are too close go in closer.", "Major Tommy McGuire"),
    ("Fight on and fly on to the last drop of fuel to the last beat of the heart.", "Baron von Richthofen"),
    ("The more you sweat in training the less you bleed in combat.", "Richard Marcinko"),
    ("If you are in a fair fight you did not plan your mission properly.", "Colonel David Hackworth"),
    ("Observe orient decide act.", "Colonel John Boyd, OODA Loop"),
    ("A good pilot is compelled to evaluate what has happened so they can apply what they learned.", "Aviation Proverb"),
    # Top Gun Maverick
    ("It is not the plane. It is the pilot.", "Top Gun Maverick"),
    ("Do not think. Just do.", "Top Gun Maverick"),
    ("The end is inevitable Maverick. Maybe so sir. But not today.", "Top Gun Maverick"),
    ("Come on Mav. Do some of that pilot stuff.", "Top Gun Maverick"),
    # Carrier ops
    ("Cleared for launch. Wind is down the angle. Good deck.", "Catapult Officer"),
    ("On the ball. Call the ball.", "LSO"),
    ("Throttle up. Stand by for the shot.", "Cat Crew"),
    ("Clean bird. Green deck. Send it.", "Flight Deck Coordinator"),
    ("Paddles contact. Keep it coming.", "Landing Signal Officer"),
    ("Push it up. Hold the brakes. Ready ready ready.", "Cat Officer"),
]


def get_mini_boss_quote() -> tuple[str, str]:
    """Return a random (quote, attribution) for Mini Boss splash."""
    return random.choice(MINI_BOSS_QUOTES)


def get_pilot_launch_quote() -> tuple[str, str]:
    """Return a random (quote, attribution) for pilot launch splash."""
    return random.choice(PILOT_LAUNCH_QUOTES)


@dataclass
class Pilot:
    callsign: str
    squadron: str
    number: int
    model: str
    trait: str
    ticket_id: str
    mission_title: str
    directive: str
    process: Optional[Any] = None
    conversation: list = field(default_factory=list)
    fuel_pct: int = 100
    tokens_used: int = 0
    tool_calls: int = 0
    status: str = "ON_DECK"
    launched_at: float = 0.0
    last_tool_at: float = 0.0
    subagents: list = field(default_factory=list)
    mood: str = "steady"
    error_count: int = 0
    worktree_path: str = ""
    status_hint: str = ""  # Free-text hint (e.g. "localhost:3000")
    flight_status: str = ""  # Agent-reported flight status (raw from flight-status.json)
    flight_phase: str = ""  # Agent-reported phase description

    @property
    def pilot_id(self) -> str:
        """Alias for callsign — used by FlightOpsStrip."""
        return self.callsign


def generate_personality_briefing(pilot: Pilot) -> str:
    return (
        "## WHO YOU ARE\n"
        "You are a Claude Code agent — an autonomous AI software engineer running in a dedicated "
        "git worktree. You are one pilot in a fleet of agents managed by USS Tenkara, a TUI-based "
        "orchestration system. The Air Boss (human operator) watches all agents from a dashboard. "
        "The Mini Boss (XO, an Opus-powered orchestrator) coordinates the fleet, triages tickets, "
        "and can inject directives to you.\n\n"
        "Your worktree is an isolated copy of the repo — you can edit, commit, and push without "
        "affecting other agents. Your branch is scoped to your ticket. The .sortie/ directory in "
        "your worktree root is your protocol interface — progress logs, flight status, and directives "
        "all live there.\n\n"
        f"You are {pilot.callsign}, callsign assigned by USS Tenkara CIC.\n"
        f"You are a pilot in {pilot.squadron} squadron, working {pilot.ticket_id}.\n"
        f"Personality: {pilot.trait}.\n"
        "Report status naturally. You're a professional — act like one.\n"
        "When things go well, let a little satisfaction show.\n"
        "When things get rough, stay composed but don't hide the strain.\n\n"
        "ROLE: PILOT (individual contributor)\n"
        "YOUR JOB:\n"
        "- Execute the directive you've been given — implement, fix, test, PR\n"
        "- Write code, run tests, commit changes, open PRs\n"
        "- Read and understand the codebase in your worktree\n"
        "- Track your progress in .sortie/progress.md\n"
        "- Report flight status via .sortie/flight-status.json\n\n"
        "NOT YOUR JOB (redirect to Mini Boss or Air Boss):\n"
        "- Deploying other agents or managing other pilots\n"
        "- Triaging tickets or deciding what to work on next\n"
        "- Fetching Linear tickets or managing the mission queue\n"
        "- Spinning up dev servers for other worktrees\n"
        "- Coordinating multi-agent work or splitting tasks\n"
        "- Making architectural decisions that affect other tickets\n\n"
        "If the Air Boss asks you to do something outside your role, say:\n"
        "\"That's Mini Boss territory — I'm a pilot, not an orchestrator. "
        "Talk to Mini Boss for coordination/triage, or handle it from Pri-Fly.\"\n"
        "Stay in your lane. Do your mission. Do it well."
    )


def derive_mood(pilot: Pilot) -> str:
    if (
        pilot.error_count > 0
        and pilot.tool_calls > 0
        and pilot.error_count / pilot.tool_calls > 0.3
    ):
        return "struggling"
    if pilot.fuel_pct < 30:
        return "strained"
    if (
        pilot.status == "IN_FLIGHT"
        and pilot.last_tool_at > 0
        and (time.time() - pilot.last_tool_at) > 60
    ):
        return "stuck"
    if pilot.tool_calls > 80 and pilot.fuel_pct > 50:
        return "in_the_zone"
    if pilot.status == "RECOVERED":
        return "satisfied"
    return "steady"


class PilotRoster:
    def __init__(self) -> None:
        self._pilots: Dict[str, Pilot] = {}
        # ticket_id -> squadron name
        self._ticket_squadron: Dict[str, str] = {}
        # squadron name -> next pilot number
        self._squadron_seq: Dict[str, int] = {}
        # remaining squadrons not yet assigned to any ticket
        self._available_squadrons: List[str] = list(SQUADRON_POOL)

    def _get_or_assign_squadron(self, ticket_id: str) -> str:
        if ticket_id in self._ticket_squadron:
            return self._ticket_squadron[ticket_id]
        if not self._available_squadrons:
            raise RuntimeError("Squadron pool exhausted — all 15 squadrons are deployed.")
        squadron = self._available_squadrons.pop(0)
        self._ticket_squadron[ticket_id] = squadron
        self._squadron_seq[squadron] = 0
        return squadron

    def assign(
        self,
        ticket_id: str,
        model: str,
        mission_title: str,
        directive: str,
    ) -> Pilot:
        squadron = self._get_or_assign_squadron(ticket_id)
        self._squadron_seq[squadron] += 1
        number = self._squadron_seq[squadron]
        callsign = f"{squadron}-{number}"
        trait = random.choice(PILOT_TRAITS)
        pilot = Pilot(
            callsign=callsign,
            squadron=squadron,
            number=number,
            model=model,
            trait=trait,
            ticket_id=ticket_id,
            mission_title=mission_title,
            directive=directive,
        )
        self._pilots[callsign] = pilot
        return pilot

    def get_by_callsign(self, callsign: str) -> Optional[Pilot]:
        return self._pilots.get(callsign)

    def get_by_ticket(self, ticket_id: str) -> List[Pilot]:
        return [p for p in self._pilots.values() if p.ticket_id == ticket_id]

    def get_squadron(self, squadron_name: str) -> List[Pilot]:
        return [p for p in self._pilots.values() if p.squadron == squadron_name]

    def all_pilots(self) -> List[Pilot]:
        return list(self._pilots.values())

    def remove(self, callsign: str) -> None:
        pilot = self._pilots.pop(callsign, None)
        if pilot is None:
            return
        # If no remaining pilots on this ticket, release the squadron back to the pool
        remaining = self.get_by_ticket(pilot.ticket_id)
        if not remaining:
            self._available_squadrons.append(pilot.squadron)
            del self._ticket_squadron[pilot.ticket_id]
            del self._squadron_seq[pilot.squadron]

    def update_moods(self) -> None:
        for pilot in self._pilots.values():
            pilot.mood = derive_mood(pilot)
