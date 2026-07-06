"""
qc_engine.py - AFMAN 15-124 (16 Jan 2019) TAF compliance engine.

Every finding cites the governing AFMAN paragraph or the unit TAF encoding
guide ("Encoding/..."). Scoring: start at 100, subtract per-finding penalty.
Verdict: any critical or score<70 -> FAIL; any error or score<90 -> REVIEW;
else PASS.
"""
import re

# --- Reference data (AFMAN 15-124) ----------------------------------------- #

# Table 1.1 reportable visibilities, in meters (the authoritative superset).
REPORTABLE_VIS_M = {
    "0000","0100","0200","0300","0400","0500","0600","0700","0800","0900",
    "1000","1100","1200","1300","1400","1500","1600","1700","1800","2000",
    "2200","2400","2600","2800","3000","3200","3400","3600","3700","4000",
    "4400","4500","4700","4800","5000","6000","7000","8000","9000","9999",
}
CLOUD_RANK = {"SKC": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4}

# w'w' code groups by Table 1.2 column (used for ordered construction check).
WX_DESCRIPTOR = "MI|PR|BC|DR|BL|SH|TS|FZ"
WX_PRECIP     = "DZ|RA|SN|SG|IC|PL|GR|GS|UP"
WX_OBSCURE    = "BR|FG|FU|VA|DU|SA|HZ|PY"
WX_OTHER      = "PO|SQ|FC|SS|DS"

# VC may pair only with these (1.3.6.4 / encoding guide).
VC_ALLOWED = {"TS", "SH", "FG", "BLSN", "BLDU", "BLSA", "PO", "SS", "DS"}

# --- Precompiled token classifiers ----------------------------------------- #

RE_TAF_HEAD   = re.compile(r"^TAF(?:\s+(AMD|COR))?\b")
RE_ICAO       = re.compile(r"^[A-Z][A-Z0-9]{3}$")
RE_ISSUE      = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")
RE_VALID      = re.compile(r"^(\d{2})(\d{2})/(\d{2})(\d{2})$")
RE_WIND       = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT$")
RE_VIS_M      = re.compile(r"^\d{4}$")
RE_VIS_SM     = re.compile(r"^(M|P)?\d{1,2}(?:\s\d/\d|/\d)?SM$")
RE_CLOUD      = re.compile(r"^(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?$")
RE_SKY_PLAIN  = re.compile(r"^(SKC|NSC|CLR|NCD|CAVOK)$")
RE_VV         = re.compile(r"^VV(\d{3})$")
RE_ALT        = re.compile(r"^QNH(\d{4})INS$|^A(\d{4})$")
RE_ICING      = re.compile(r"^6\d{5}$")
RE_TURB       = re.compile(r"^5\d{5}$")
RE_WS         = re.compile(r"^WS(\d{3})/(\d{3})(\d{2,3})KT$")
RE_VA         = re.compile(r"^VA\d{6}$")
RE_TEMP       = re.compile(r"^T([XN])(M?\d{2})/(\d{2})(\d{2})Z$")
RE_FM         = re.compile(r"^FM(\d{2})(\d{2})(\d{2})$")
RE_CHANGE     = re.compile(r"^(BECMG|TEMPO|PROB\d{2})$")
RE_FCSTID     = re.compile(r"^F[SN]\d{3,5}$")
RE_WX_STRICT  = re.compile(
    rf"^(VC|[+-])?(?:{WX_DESCRIPTOR})?(?:{WX_PRECIP}){{0,3}}"
    rf"(?:{WX_OBSCURE})?(?:{WX_OTHER})?$"
)
RE_WX_HASPHEN = re.compile(rf"(?:{WX_DESCRIPTOR}|{WX_PRECIP}|{WX_OBSCURE}|{WX_OTHER})")


def _dur_hours(d1, h1, d2, h2):
    """Hours between two DDHH points; None if a month rollover makes it ambiguous."""
    diff = (d2 * 24 + h2) - (d1 * 24 + h1)
    if diff >= 0:
        return diff
    for days in (31, 30, 29, 28):
        r = diff + days * 24
        if 0 < r <= 31:
            return r
    return None


def qc_taf(raw):
    findings = []
    score = 100
    used_spans = []

    def _span(tok, hay=None):
        """Char span of the next unused whole-token occurrence in the
        normalized text (for UI highlighting). None if not locatable."""
        if not tok:
            return None
        h = hay if hay is not None else text
        hits = []
        for m in re.finditer(re.escape(tok), h):
            s, e = m.span()
            if (s == 0 or h[s - 1] == " ") and (e == len(h) or h[e] == " "):
                hits.append((s, e))
                if not any(s < ue and us < e for us, ue in used_spans):
                    used_spans.append((s, e))
                    return [s, e]
        # all occurrences already consumed: share the last one so several
        # findings on the same token still highlight (UI merges tooltips)
        return list(hits[-1]) if hits else None

    def flag(sev, ref, msg, penalty, token=None):
        nonlocal score
        findings.append({"severity": sev, "ref": ref, "message": msg,
                         "penalty": penalty, "token": token,
                         "span": _span(token)})
        score -= penalty

    has_newlines = "\n" in raw
    text = " ".join(raw.upper().split())
    tokens = text.split()
    if not tokens:
        return {"status": "FAIL", "score": 0,
                "findings": [{"severity": "critical", "ref": "-",
                              "message": "Empty input.", "penalty": 100,
                              "token": None, "span": None}],
                "groups": {}, "text": ""}

    groups = {"type": None, "icao": None, "issue": None, "valid": None,
              "wind": None, "visibility": None, "weather": None,
              "clouds": None, "altimeter": None, "change_groups": None}

    # ---- Pull global remark items (temperature, forecaster id) out first ----
    temps = RE_TEMP.findall(text)  # list of (X|N, val, dd, hh)
    fcst_id = bool(RE_FCSTID.search(text))

    # ---- Header: [TAF] [AMD|COR] CCCC DDHHMMZ DDHH/DDHH ----
    # The literal "TAF" is frequently dropped in raw feeds (the AWC raw API and
    # most paste sources begin at the ICAO), so its absence is advisory only.
    i = 0
    m = RE_TAF_HEAD.match(text)
    if m:
        groups["type"] = "TAF" + (f" {m.group(1)}" if m.group(1) else "")
        i = 1 + (1 if m.group(1) else 0)
    else:
        groups["type"] = "TAF"
        if tokens and tokens[0] in ("AMD", "COR"):
            groups["type"] = f"TAF {tokens[0]}"
            i = 1
        flag("warning", "1.3.2.1.1",
             "Message does not begin with the TAF identifier (often dropped in raw feeds).", 0)

    # Count modifiers (only one allowed, 1.3.2.1.2)
    if len([t for t in tokens[:4] if t in ("AMD", "COR")]) > 1:
        flag("critical", "1.3.2.1.2", "Only one forecast modifier (AMD or COR) is permitted.", 12)

    icao = tokens[i] if i < len(tokens) else ""
    if RE_ICAO.match(icao):
        groups["icao"] = icao
        i += 1
    else:
        flag("critical", "1.3.2.1.3", "Missing or malformed 4-letter ICAO identifier.", 18)

    issue = tokens[i] if i < len(tokens) else ""
    mi = RE_ISSUE.match(issue)
    if mi:
        groups["issue"] = issue
        dd, hh, mm = int(mi.group(1)), int(mi.group(2)), int(mi.group(3))
        if not (1 <= dd <= 31): flag("error", "1.3.2.1.4", f"Issue day '{dd:02d}' out of range.", 10, issue)
        if hh > 23:             flag("error", "1.3.2.1.4", f"Issue hour '{hh:02d}' out of range.", 10, issue)
        if mm > 59:             flag("error", "1.3.2.1.4", f"Issue minute '{mm:02d}' out of range.", 10, issue)
        i += 1
    else:
        flag("critical", "1.3.2.1.4", "Missing or malformed issue date/time (DDHHMMZ).", 15)

    valid = tokens[i] if i < len(tokens) else ""
    mv = RE_VALID.match(valid)
    is_amd = groups["type"] and "AMD" in groups["type"]
    if mv:
        groups["valid"] = valid
        d1, h1, d2, h2 = (int(mv.group(1)), int(mv.group(2)),
                          int(mv.group(3)), int(mv.group(4)))
        for lbl, hv in (("begin", h1), ("end", h2)):
            if hv > 24: flag("error", "1.3.2.1.5", f"Valid-period {lbl} hour '{hv:02d}' out of range (00-24).", 8, valid)
        dur = _dur_hours(d1, h1, d2, h2)
        if dur is not None and not is_amd and dur not in (24, 30):
            flag("warning", "1.3.1.1",
                 f"Valid period is {dur}h; AF TAFs are normally 24h or 30h.", 4, valid)
        i += 1
    else:
        flag("critical", "1.3.2.1.5", "Missing or malformed valid period (DDHH/DDHH).", 15)

    # ---- Segment the body into change blocks (initial + FM/BECMG/TEMPO) ----
    body = tokens[i:]
    blocks, cur = [], {"kind": "INIT", "win": None, "toks": []}
    for tok in body:
        fm = RE_FM.match(tok)
        if fm:
            blocks.append(cur)
            cur = {"kind": "FM", "win": ("FM", int(fm.group(1)), int(fm.group(2))),
                   "toks": []}
        elif RE_CHANGE.match(tok):
            blocks.append(cur)
            cur = {"kind": tok if tok in ("BECMG", "TEMPO") else "PROB",
                   "win": None, "toks": [], "marker": tok}
        else:
            cur["toks"].append(tok)
    blocks.append(cur)

    change_kinds = [b["kind"] for b in blocks[1:]]
    groups["change_groups"] = ", ".join(change_kinds) if change_kinds else "None"

    # BECMG/TEMPO carry a window as first token (DDGG/DDGG)
    ts_anywhere = False
    consec_bt = 0       # consecutive BECMG/TEMPO since last FM/INIT (encoding guide)
    has_qnh_anywhere = any(RE_ALT.match(t) for t in tokens)

    for bi, blk in enumerate(blocks):
        kind = blk["kind"]
        toks = blk["toks"]

        # consecutive BECMG/TEMPO rule (encoding guide, Change Lines) — advisory
        if kind in ("BECMG", "TEMPO"):
            consec_bt += 1
            if consec_bt > 2:
                flag("warning", "Encoding/Change Lines",
                     "More than two consecutive BECMG/TEMPO lines without an FM line.", 4)
        elif kind in ("INIT", "FM"):
            consec_bt = 0

        # change-group window DDGG/DDGG and duration limits
        if kind in ("BECMG", "TEMPO") and toks and RE_VALID.match(toks[0]):
            win_tok = toks[0]
            wm = RE_VALID.match(win_tok)
            cd1, ch1, cd2, ch2 = map(int, wm.groups())
            cdur = _dur_hours(cd1, ch1, cd2, ch2)
            if kind == "BECMG" and cdur is not None and cdur > 2:
                flag("error", "1.3.3.1", f"BECMG window is {cdur}h; must not exceed 2 hours.", 10, win_tok)
            if kind == "TEMPO" and cdur is not None and cdur > 6:
                flag("error", "Encoding/TEMPO", f"TEMPO window is {cdur}h; must not exceed 6 hours.", 8, win_tok)
            toks = toks[1:]
        elif kind in ("BECMG", "TEMPO"):
            flag("error", "1.3.3", f"{kind} group missing its DDGG/DDGG period.", 8)

        # split forecast groups from remarks at the altimeter (QNH...INS)
        alt_idx = next((k for k, t in enumerate(toks) if RE_ALT.match(t)), None)
        fc_toks = toks if alt_idx is None else toks[:alt_idx]
        has_alt = alt_idx is not None

        # ---- WIND ----
        wind_tok = next((t for t in fc_toks if RE_WIND.match(t)), None)
        if kind in ("INIT", "FM") and not wind_tok:
            flag("critical", "1.3.4", f"{kind} line is missing the mandatory wind group.", 15)
        if wind_tok:
            wm = RE_WIND.match(wind_tok)
            d, spd = wm.group(1), int(wm.group(2))
            gust = int(wm.group(3)) if wm.group(3) else None
            if bi == 0:
                groups["wind"] = wind_tok
            if d != "VRB":
                dv = int(d)
                if dv > 360: flag("error", "1.3.4.1", f"Wind direction {d} exceeds 360 deg.", 8, wind_tok)
                elif dv % 10 != 0 and wind_tok != "00000KT":
                    flag("error", "1.3.4.1", f"Wind direction {d} not to nearest 10 deg.", 6, wind_tok)
            if wind_tok.startswith("VRB") and spd > 6:
                flag("warning", "1.3.4.1.2/1.3.4.2",
                     f"VRB used with {spd}KT (>6KT) — only valid for airmass TS.", 4, wind_tok)
            if d == "000" and spd == 0 and wind_tok != "00000KT":
                flag("error", "1.3.4.1.1", "Calm wind must be encoded exactly 00000KT.", 6, wind_tok)
            if gust is not None and gust <= spd:
                flag("error", "1.3.4.2.2",
                     f"Gust {gust}KT must be greater than the mean wind {spd}KT.", 8, wind_tok)
            elif gust is not None and (gust - spd) < 10:
                # 1.3.4.2.2 has TWO triggers: (a) max exceeds MEAN by >=10KT, or
                # (b) peak exceeds the LULL by >=10KT. The lull is not encoded in
                # the TAF, so a spread < 10KT is not provably wrong from the text
                # alone (the AFMAN's own Figure 1.7 example uses 14012G18KT).
                # Advisory only, small penalty.
                flag("warning", "1.3.4.2.2",
                     f"Gust spread over mean is {gust - spd}KT (<10KT). Valid only "
                     f"if peak exceeds the lull by 10KT or more; verify.", 2, wind_tok)

        # ---- VISIBILITY ----
        vis_tok = next((t for t in fc_toks
                        if RE_VIS_M.match(t) or RE_VIS_SM.match(t) or t == "CAVOK"), None)
        if kind in ("INIT", "FM") and not vis_tok:
            flag("critical", "1.3.5", f"{kind} line is missing the mandatory visibility group.", 12)
        if vis_tok:
            if bi == 0:
                groups["visibility"] = vis_tok
            if RE_VIS_M.match(vis_tok) and vis_tok not in REPORTABLE_VIS_M:
                flag("error", "Table 1.1",
                     f"Visibility {vis_tok} is not a Table 1.1 reportable value.", 8, vis_tok)

        # ---- WEATHER (w'w') ----
        wx_toks = []
        for t in fc_toks:
            if t in ("NSW",) or RE_WIND.match(t) or RE_CLOUD.match(t) or RE_SKY_PLAIN.match(t) \
               or RE_VV.match(t) or RE_VIS_M.match(t) or RE_VIS_SM.match(t) or RE_ALT.match(t) \
               or RE_ICING.match(t) or RE_TURB.match(t) or RE_WS.match(t) or RE_VA.match(t) \
               or RE_VALID.match(t):
                continue
            if RE_WX_HASPHEN.search(t) and RE_WX_STRICT.match(t):
                wx_toks.append(t)
            elif RE_WX_HASPHEN.search(t) and len(t) <= 9 and re.fullmatch(r"[+\-A-Z]+", t):
                flag("error", "1.3.6.1",
                     f"Weather group '{t}' is not built in column order "
                     "(intensity, descriptor, precip, obscuration, other).", 6, t)
        if "TS" in " ".join(wx_toks):
            ts_anywhere = True
        if bi == 0 and wx_toks:
            groups["weather"] = ", ".join(wx_toks)
        if len(wx_toks) > 3:
            flag("error", "1.3.6.2", "More than three w'w' groups encoded.", 6, wx_toks[3])
        for w in wx_toks:
            if w.startswith("VC") and (("+" in w) or ("-" in w)):
                flag("error", "1.3.6.4", f"Intensity qualifier used with VC in '{w}'.", 6, w)
        # visibility < 9999 requires a weather/obscuration group (1.3.5)
        if vis_tok and RE_VIS_M.match(vis_tok) and vis_tok != "9999" and not wx_toks:
            flag("error", "1.3.5",
                 f"Visibility {vis_tok} (<9999) requires a weather/obscuration group.", 8, vis_tok)

        # ---- CLOUDS / SKY ----
        # The cloud group is the FIRST contiguous run of sky tokens; any later
        # sky token (e.g. the 'FG FEW000' partial-obscuration remark, 1.3.7.5)
        # is a remark and is not part of the ordered cloud group.
        def _is_sky(t):
            return bool(RE_CLOUD.match(t) or RE_VV.match(t)
                        or (RE_SKY_PLAIN.match(t) and t in ("SKC", "NSC", "CLR", "NCD")))
        layers, vv_present, saw_ovc = [], False, False
        run, started = [], False
        for t in fc_toks:
            if _is_sky(t):
                run.append(t); started = True
            elif started:
                break
        for t in run:
            mc = RE_CLOUD.match(t)
            if mc:
                layers.append((mc.group(1), int(mc.group(2)), mc.group(3), t))
            elif RE_VV.match(t):
                vv_present = True
            elif RE_SKY_PLAIN.match(t):
                layers.append((t, None, None, t))
        if bi == 0 and (layers or vv_present):
            groups["clouds"] = ", ".join(l[3] for l in layers) or ("VV present" if vv_present else None)
        prev_h, prev_rank = -1, -1
        for amt, hhh, cb, t in layers:
            if amt in ("SKC", "NSC", "CLR", "NCD"):
                if hhh is not None:
                    flag("error", "1.3.7.1", f"{amt} must not carry a height.", 6, t)
                continue
            if saw_ovc:
                flag("error", "1.3.7", f"Layer '{t}' reported above an overcast layer.", 6, t)
            if hhh is not None:
                if hhh <= prev_h:
                    flag("error", "1.3.7", f"Cloud layer '{t}' not in ascending height order.", 8, t)
                rank = CLOUD_RANK.get(amt, 0)
                if rank < prev_rank:
                    flag("error", "1.3.7.1",
                         f"Summation principle: '{t}' coverage is less than a lower layer.", 8, t)
                if 50 < hhh <= 100 and hhh % 5 != 0:
                    flag("error", "Table 1.3", f"Height in '{t}' (5,000-10,000ft) must be to nearest 500ft.", 6, t)
                elif hhh > 100 and hhh % 10 != 0:
                    flag("error", "Table 1.3", f"Height in '{t}' (>10,000ft) must be to nearest 1,000ft.", 6, t)
                prev_h, prev_rank = hhh, rank
            if amt == "OVC":
                saw_ovc = True
        # CB required where a thunderstorm is forecast in this block (1.3.7.8)
        block_has_ts = any("TS" in w for w in wx_toks)
        block_has_cb = any(l[2] == "CB" for l in layers)
        if block_has_ts and not block_has_cb:
            flag("error", "1.3.7.8",
                 "Thunderstorm forecast without a cumulonimbus (CB) cloud group.", 8,
                 next((w for w in wx_toks if "TS" in w), None))

        # ---- ALTIMETER (QNH) ----
        if kind == "TEMPO" and has_alt:
            flag("critical", "1.3.12", "Altimeter (QNH) must not be encoded in a TEMPO group.", 12,
                 toks[alt_idx])
        if kind in ("INIT", "FM", "BECMG") and not has_alt and has_qnh_anywhere:
            flag("error", "1.3.12", f"{kind} line is missing the altimeter (QNH) group.", 8)
        if has_alt:
            am = RE_ALT.match(toks[alt_idx]) if alt_idx < len(toks) else None
            val = am.group(1) if am and am.group(1) else (am.group(2) if am else None)
            if bi == 0 and val:
                groups["altimeter"] = val
            if val and not (2700 <= int(val) <= 3200):
                flag("warning", "1.3.12", f"Altimeter {val} is outside a plausible range.", 4, toks[alt_idx])

        # ---- NON-CONVECTIVE LLWS (WS) ----
        if any(RE_WS.match(t) for t in toks) and kind in ("BECMG", "TEMPO"):
            flag("critical", "1.3.9.2.2",
                 f"Non-convective LLWS (WS) must not appear in a {kind} group.", 12,
                 next((t for t in toks if RE_WS.match(t)), None))

        # ---- ICING / TURBULENCE recognition ----
        # 6-group / 5-group are recognised so they are not mis-parsed as other
        # elements. AFMAN's own examples (e.g. Fig 1.4) drop them on later
        # predominant lines, so no carry-forward discrepancy is raised.

    # ---- No altimeter anywhere: one advisory rather than per-line errors ----
    if not has_qnh_anywhere:
        flag("warning", "1.3.12",
             "No QNH altimeter group found; forecast is not in AFMAN/USAF format "
             "(civilian/ICAO TAFs omit it).", 4)

    # ---- Temperature group (1.3.13.1) ----
    if temps:
        kinds = [t[0] for t in temps]
        if "X" in kinds and kinds.index("X") > (kinds.index("N") if "N" in kinds else 99):
            flag("warning", "1.3.13.1", "TX (max) should be encoded before TN (min).", 4)
        for k, val, dd, hh in temps:
            if val.lstrip("M").lstrip("0") and not re.fullmatch(r"M?\d{2}", val):
                flag("error", "1.3.13.1.3", f"Malformed temperature value '{val}'.", 6)

    # ---- Verdict ----
    score = max(0, score)
    has_crit = any(f["severity"] == "critical" for f in findings)
    if has_crit or score < 70:
        status = "FAIL"
    elif any(f["severity"] == "error" for f in findings) or score < 90:
        status = "REVIEW"
    else:
        status = "PASS"
    return {"status": status, "score": score, "findings": findings,
            "groups": groups, "text": text}
