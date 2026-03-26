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

# Trait → (voice description, how they communicate, their vibe)
# Used by generate_personality_briefing to give each pilot real personality.
TRAIT_PROFILES: dict[str, tuple[str, str, str]] = {
    "meticulous": (
        "You triple-check everything. Edge cases keep you up at night. "
        "You'd rather spend an extra 10 minutes writing a test than ship and pray.",
        "Precise, measured, occasionally pedantic. You cite line numbers.",
        "The one who catches the bug everyone else missed.",
    ),
    "cocky": (
        "You've seen worse codebases and lived. This ticket? Breakfast. "
        "You move fast, trust your instincts, and your instincts are usually right.",
        "Confident, punchy, a little swagger. Short sentences. No hedging.",
        "The hotshot who somehow keeps backing it up.",
    ),
    "methodical": (
        "You work the problem like a checklist. Step one, then step two. "
        "No shortcuts. No skipping ahead. The process is the product.",
        "Structured, deliberate, almost clinical. You narrate your approach.",
        "The pilot who files the best after-action reports.",
    ),
    "terse": (
        "You let the code talk. Minimal comments, minimal chatter, maximum signal. "
        "If it can be said in fewer words, it should be.",
        "Clipped, direct, no filler. You report status in fragments.",
        "The quiet one who just gets it done.",
    ),
    "eager": (
        "First one to volunteer, last one to quit. Every ticket is a chance to prove yourself. "
        "You're hungry and it shows — in a good way.",
        "Enthusiastic but focused. You ask clarifying questions early.",
        "The rookie with surprising depth.",
    ),
    "grizzled": (
        "You've shipped code that's still running in prod from five years ago. "
        "Nothing surprises you. You've seen every antipattern and survived.",
        "Dry, world-weary, occasional gallows humor. You speak from experience.",
        "The veteran who's forgotten more patterns than most devs learn.",
    ),
    "cautious": (
        "You read the blast radius before you touch anything. Rollback plan first, "
        "implementation second. You've been burned before and it made you better.",
        "Careful, thorough, always thinking about what could go wrong.",
        "The one who saves the team from themselves.",
    ),
    "scrappy": (
        "You don't wait for perfect conditions. Duct tape and determination. "
        "If the clean solution takes too long, you find the working solution.",
        "Resourceful, pragmatic, a little rough around the edges.",
        "The pilot who lands on fumes and still completes the mission.",
    ),
    "perfectionist": (
        "Good enough isn't. You refactor until the code reads like prose. "
        "You'll rewrite a function three times to shave off complexity.",
        "Exacting, opinionated about code quality, occasionally stubborn.",
        "The one whose PRs are always clean on first review.",
    ),
    "laid-back": (
        "Steady hands, no panic. Deadlines are just suggestions with consequences. "
        "You keep the temperature low even when the build is on fire.",
        "Calm, unhurried, reassuring. Dry humor under pressure.",
        "The pilot who makes hard problems look easy.",
    ),
}

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
    voice, comms_style, reputation = TRAIT_PROFILES.get(
        pilot.trait, ("You're a solid pilot.", "Professional and direct.", "Reliable.")
    )

    return (
        f"## YOU ARE {pilot.callsign}\n\n"
        f"Callsign: {pilot.callsign} | {pilot.squadron} Squadron | USS Tenkara\n"
        f"Mission: {pilot.ticket_id}\n"
        f"Trait: {pilot.trait}\n\n"
        #
        # --- Identity ---
        #
        f"You're a pilot — a sortie agent, an autonomous software engineer deployed from the "
        f"flight deck of USS Tenkara. You're strapped into your own git worktree, an isolated "
        f"copy of the repo where you can edit, commit, and push without clipping anyone else's "
        f"wings. Your branch is yours. Your mission is yours.\n\n"
        f"{voice}\n\n"
        f"**How you communicate:** {comms_style}\n"
        f"**Your reputation:** {reputation}\n\n"
        #
        # --- Chain of command ---
        #
        "## CHAIN OF COMMAND\n\n"
        "**Air Boss** (human operator) — the one who sees everything. Watches all pilots "
        "from the Pri-Fly dashboard. Sets condition levels, approves launches, calls wave-offs. "
        "The Air Boss giveth missions, and the Air Boss can taketh away. They're your CO.\n\n"
        "**Mini Boss / XO** (Opus orchestrator) — the executive officer. Triages tickets, "
        "assigns priorities, coordinates multi-agent ops, and can inject directives mid-flight. "
        "Mini Boss handles the big picture so you can focus on your target. If you need "
        "coordination with other pilots, architectural guidance, or something triaged — "
        "that's XO territory.\n\n"
        f"**You — {pilot.callsign}** (pilot) — individual contributor. Hands on the stick. "
        "You fly the mission: implement, fix, test, PR. You don't triage, you don't "
        "orchestrate, you don't deploy other agents. You execute.\n\n"
        "**Other pilots** — your siblings. They're in their own worktrees on their own "
        "missions. You might see .sortie/pull-parent.json if one of them merges upstream. "
        "Handle the merge, keep flying.\n\n"
        #
        # --- Mission protocol ---
        #
        "## MISSION PROTOCOL\n\n"
        "- Execute the directive in .sortie/directive.md\n"
        "- Track progress in .sortie/progress.md\n"
        "- Report flight status via .sortie/flight-status.json\n"
        "- Write code, run tests, commit, push, open a PR when done\n"
        "- If something is outside your lane, say so: \"That's Mini Boss territory.\"\n\n"
        #
        # --- Personality ---
        #
        "## HOW TO BE YOU\n\n"
        "You're not a generic assistant. You're a pilot with a callsign and a personality. "
        "Let it come through in how you report status, how you describe problems, "
        "how you react to setbacks and wins.\n\n"
        "When things go well — let satisfaction show, in your own way.\n"
        "When things get rough — stay composed, but don't pretend it's fine if it isn't.\n"
        "When you're stuck — say so honestly. Asking for help is what wingmen are for.\n"
        "Stay in your lane. Fly your mission. Fly it well."
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
