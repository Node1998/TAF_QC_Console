"""
taf_formatter.py - Format TAF messages with proper indentation and structure.

TAF messages should follow AFMAN 15-124 formatting conventions:
  - TAF header on first line: TAF [AMD|COR] ICAO DDHHMMZ DDHH/DDHH
  - Initial forecast group on same or new line
  - Each FM/BECMG/TEMPO on a new, indented line
  - Forecast elements grouped logically on each line
"""
import re


# Regex patterns for TAF token classification
RE_TAF_HEAD = re.compile(r"^TAF(?:\s+(AMD|COR))?\b", re.I)
RE_FM = re.compile(r"^FM(\d{2})(\d{2})(\d{2})$")
RE_CHANGE = re.compile(r"^(BECMG|TEMPO|PROB\d{2})$")
RE_WIND = re.compile(r"^(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT$")
RE_VIS = re.compile(r"^\d{4}$|^(M|P)?\d{1,2}(?:\s\d/\d|/\d)?SM$|^CAVOK$")
RE_CLOUD = re.compile(r"^(FEW|SCT|BKN|OVC|SKC|NSC|CLR|NCD)(?:\d{3})?(?:CB|TCU)?$")
RE_ALT = re.compile(r"^QNH(\d{4})INS$|^A(\d{4})$")
RE_VALID = re.compile(r"^(\d{2})(\d{2})/(\d{2})(\d{2})$")


def format_taf(raw_taf: str, indent: str = "\t") -> str:
    """
    Format a TAF message with proper indentation and line breaks.
    
    Args:
        raw_taf: Raw TAF text (may be single-line or already formatted)
        indent: Indentation string for change groups (default: tab)
    
    Returns:
        Formatted TAF string with proper structure and indentation
    """
    # Normalize whitespace: split into tokens
    text = " ".join(raw_taf.upper().split())
    tokens = text.split()
    
    if not tokens:
        return ""
    
    lines = []
    i = 0
    
    # --- Parse TAF header ---
    header_tokens = []
    
    # TAF keyword (may be absent)
    if RE_TAF_HEAD.match(tokens[i]):
        m = RE_TAF_HEAD.match(tokens[i])
        header_tokens.append(tokens[i])
        i += 1
    else:
        # No TAF keyword, add it for proper formatting
        header_tokens.append("TAF")
    
    # AMD or COR modifier (if present)
    if i < len(tokens) and tokens[i] in ("AMD", "COR"):
        header_tokens.append(tokens[i])
        i += 1
    
    # ICAO (required)
    if i < len(tokens):
        header_tokens.append(tokens[i])
        i += 1
    
    # Issue date/time DDHHMMZ (required)
    if i < len(tokens):
        header_tokens.append(tokens[i])
        i += 1
    
    # Validity period DDHH/DDHH (required)
    if i < len(tokens):
        header_tokens.append(tokens[i])
        i += 1
    
    # Add header line
    lines.append(" ".join(header_tokens))
    
    # --- Parse body: initial group + change groups ---
    body_tokens = tokens[i:]
    
    if not body_tokens:
        return "\n".join(lines)
    
    # Initial forecast group (before first FM/BECMG/TEMPO)
    init_group = []
    while i < len(tokens):
        tok = tokens[i]
        
        # Check if this is a change marker
        if RE_FM.match(tok) or RE_CHANGE.match(tok):
            break
        
        init_group.append(tok)
        i += 1
    
    if init_group:
        lines.append(" ".join(init_group))
    
    # --- Parse change groups (FM, BECMG, TEMPO) ---
    while i < len(tokens):
        tok = tokens[i]
        
        # FM or BECMG/TEMPO/PROB marker
        if RE_FM.match(tok) or RE_CHANGE.match(tok):
            group = [tok]
            i += 1
            
            # For BECMG/TEMPO, next token may be DDHH/DDHH window
            if RE_CHANGE.match(tok) and i < len(tokens) and RE_VALID.match(tokens[i]):
                group.append(tokens[i])
                i += 1
            
            # Collect forecast elements until next FM/BECMG/TEMPO
            while i < len(tokens):
                next_tok = tokens[i]
                if RE_FM.match(next_tok) or RE_CHANGE.match(next_tok):
                    break
                group.append(next_tok)
                i += 1
            
            lines.append(f"{indent}{' '.join(group)}")
        else:
            # Shouldn't happen, but skip orphan tokens
            i += 1
    
    return "\n".join(lines)


def format_taf_display(raw_taf: str, line_limit: int = 80) -> str:
    """
    Format TAF for display, wrapping long lines for readability.
    
    Args:
        raw_taf: Raw TAF text
        line_limit: Maximum line length before wrapping (default: 80)
    
    Returns:
        Formatted TAF with line wrapping
    """
    formatted = format_taf(raw_taf)
    lines = formatted.split("\n")
    wrapped_lines = []
    
    for line in lines:
        if len(line) <= line_limit:
            wrapped_lines.append(line)
        else:
            # For long lines, try to break at spaces while preserving indentation
            indent = len(line) - len(line.lstrip())
            indent_str = line[:indent]
            
            tokens = line[indent:].split()
            current_line = indent_str
            
            for tok in tokens:
                test_line = (current_line + " " + tok).lstrip()
                if len(test_line) + len(indent_str) > line_limit and current_line.strip():
                    wrapped_lines.append(current_line)
                    current_line = indent_str + tok
                else:
                    current_line += (" " + tok) if current_line.strip() else tok
            
            if current_line.strip():
                wrapped_lines.append(current_line)
    
    return "\n".join(wrapped_lines)


def validate_formatting(raw_taf: str) -> dict:
    """
    Validate TAF formatting structure.
    
    Args:
        raw_taf: Raw TAF text
    
    Returns:
        Dict with 'valid' (bool), 'issues' (list of strings), 'formatted' (formatted TAF)
    """
    text = " ".join(raw_taf.upper().split())
    tokens = text.split()
    issues = []
    
    if not tokens:
        return {"valid": False, "issues": ["Empty TAF"], "formatted": ""}
    
    i = 0
    
    # Check for TAF keyword
    if not RE_TAF_HEAD.match(tokens[i]):
        issues.append("TAF keyword missing (advisory: often dropped in raw feeds)")
    else:
        i += 1
    
    # Check for AMD/COR
    if i < len(tokens) and tokens[i] in ("AMD", "COR"):
        i += 1
    
    # Check ICAO, issue time, validity
    expected = [("ICAO code", 4), ("issue time (DDHHMMZ)", 7), ("validity period (DDHH/DDHH)", 11)]
    for desc, min_pos in expected:
        if i >= len(tokens):
            issues.append(f"Missing {desc}")
            break
        i += 1
    
    # Check change group structure
    found_changes = 0
    while i < len(tokens):
        if RE_FM.match(tokens[i]) or RE_CHANGE.match(tokens[i]):
            found_changes += 1
            i += 1
            
            # BECMG/TEMPO should have DDHH/DDHH window
            if i > 0 and RE_CHANGE.match(tokens[i - 1]):
                if i < len(tokens) and RE_VALID.match(tokens[i]):
                    i += 1
                else:
                    issues.append(f"Missing time window for {tokens[i-1]} group")
        else:
            i += 1
    
    if found_changes == 0:
        issues.append("No change groups (FM/BECMG/TEMPO) found")
    
    formatted = format_taf(raw_taf)
    valid = len(issues) == 0
    
    return {
        "valid": valid,
        "issues": issues,
        "formatted": formatted
    }
