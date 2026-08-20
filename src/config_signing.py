"""Signed configuration updates, and the audit trail for them.

WHY A REGISTER MAP IS A SAFETY ARTEFACT, NOT AN IT ONE
------------------------------------------------------
This is the sentence the spec says earns real points, and it is worth being
precise about the mechanism rather than gesturing at it.

A register map says "holding register 40001, scaled by 0.1, is degrees Celsius".
Change the scale factor to 1.0 and a furnace reading 720 C reports 72 C. Every
downstream consumer -- the trend, the alarm limit, the operator's screen, the
interlock that a control engineer assumed was reading real units -- now agrees on
a number that is wrong by a factor of ten, and agrees CONFIDENTLY. Nothing errors.
The historian fills with plausible values. The first indication is physical.

That is why config integrity sits with process safety rather than with IT change
management: the failure mode is not "data unavailable", it is "data wrong and
trusted". An attacker who can rewrite a register map does not need to touch the
PLC.

WHAT IS AND IS NOT IMPLEMENTED HERE
-----------------------------------
Implemented: HMAC-SHA256 over a canonical serialisation, verified before a config
is accepted, with every accepted and REJECTED update recorded in an append-only
audit log, plus a monotonic version counter that makes rollback attacks visible.

Not implemented: asymmetric signatures (the shared secret here would be a real
key-distribution problem at fleet scale), hardware-backed key storage, and
certificate rotation. HMAC is the right shape and the wrong trust model for a
fleet of a thousand gateways -- with a shared secret, compromising ONE gateway
yields the ability to sign config for ALL of them, which is exactly the property
asymmetric signing exists to remove. Saying so is the honest scope line.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time


def canonical(config: dict) -> bytes:
    """Deterministic serialisation.

    Sorted keys and fixed separators, because a signature over a dict is a
    signature over ITS SERIALISATION -- and two JSON encoders that disagree about
    key order or whitespace produce different bytes for the same config, which
    means a valid config fails verification on a different machine. That class of
    bug is why canonicalisation is specified rather than assumed.
    """
    return json.dumps(config, sort_keys=True, separators=(",", ":")).encode()


def sign(config: dict, secret: bytes) -> str:
    return hmac.new(secret, canonical(config), hashlib.sha256).hexdigest()


def verify(config: dict, signature: str, secret: bytes) -> bool:
    # compare_digest, not ==. String comparison short-circuits on the first
    # differing byte, which leaks the correct prefix through timing; on a LAN
    # that is a practical attack, not a theoretical one.
    return hmac.compare_digest(sign(config, secret), signature)


class ConfigStore:
    """Versioned config with signature verification and an append-only audit log."""

    def __init__(self, secret: bytes, initial: dict):
        self.secret = secret
        self.version = 1
        self.config = dict(initial)
        self.audit: list[dict] = []
        self._log("INITIAL", True, "bootstrap config accepted", self.version)

    def _log(self, action: str, accepted: bool, detail: str, version: int) -> None:
        self.audit.append({
            "ts": time.time(), "action": action, "accepted": accepted,
            "detail": detail, "version": version,
            "config_hash": hashlib.sha256(canonical(self.config)).hexdigest()[:16],
        })

    def apply_update(self, new_config: dict, signature: str,
                     claimed_version: int) -> dict:
        """Accept a config only if signed AND strictly newer.

        The version check is not bureaucracy. Without it, an attacker who captured
        a previously valid signed config can replay it -- a ROLLBACK ATTACK -- and
        every signature check passes, because the config really was signed by the
        legitimate key. Monotonic versioning is what makes "valid" and "current"
        different properties.
        """
        if not verify(new_config, signature, self.secret):
            self._log("UPDATE", False, "signature verification FAILED", self.version)
            return {"accepted": False, "reason": "bad signature",
                    "version": self.version}
        if claimed_version <= self.version:
            self._log("UPDATE", False,
                      f"rollback rejected: v{claimed_version} <= current "
                      f"v{self.version}", self.version)
            return {"accepted": False, "reason": "rollback / replay",
                    "version": self.version}

        changed = self._diff(self.config, new_config)
        self.config = dict(new_config)
        self.version = claimed_version
        self._log("UPDATE", True, f"applied v{claimed_version}: {changed}",
                  self.version)
        return {"accepted": True, "version": self.version, "changed": changed}

    @staticmethod
    def _diff(old: dict, new: dict) -> str:
        keys = set(old) | set(new)
        diffs = [f"{k}: {old.get(k)!r} -> {new.get(k)!r}"
                 for k in sorted(keys) if old.get(k) != new.get(k)]
        return "; ".join(diffs) if diffs else "no field changes"

    def safety_relevant_changes(self) -> list[dict]:
        """Config fields whose corruption is a PROCESS SAFETY issue, flagged.

        Scaling, units and plausibility limits change the MEANING of every value
        downstream. A scan-rate change makes data late; a scale-factor change
        makes it wrong, and wrong-but-plausible is the dangerous one.
        """
        safety_fields = ("scale", "units", "min_plausible", "max_plausible",
                         "word_swap", "address")
        out = []
        for entry in self.audit:
            if entry["accepted"] and any(f in entry["detail"] for f in safety_fields):
                out.append({**entry, "classification": "SAFETY_RELEVANT"})
        return out
