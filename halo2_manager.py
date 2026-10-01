import streamlit as st
import sqlite3
from datetime import datetime
import pandas as pd
import math
import random
import json
from streamlit_sortables import sort_items
import streamlit.components.v1 as _stc
import os as _os
_DND_COMPONENT = _stc.declare_component(
    "dnd_teams",
    path=_os.path.join(_os.path.dirname(__file__), "dnd_component")
)

# ---------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------
MODE_COLS = {
    "4v4": ("mmr_4v4", "rank_4v4", "pts_4v4"),
    "2v2": ("mmr_2v2", "rank_2v2", "pts_2v2"),
    "1v1": ("mmr_1v1", "rank_1v1", "pts_1v1"),
}

# Simplified MMR scale for draft audit engine (per requerimientos2.txt)
RANK_MMR_SIMPLE = {
    "D": 1.0, "D+": 1.5, "C": 2.0, "C+": 2.5,
    "B": 3.0, "B+": 3.5, "A": 4.0, "A+": 4.5,
    "S": 5.0, "S+": 5.5, "S++": 6.0, "S+++": 6.5
}

BALANCE_TOLERANCE = 0.75  # in simplified MMR units

PR_PER_MMR_STEP = 500

RANK_PR_THRESHOLDS = {
    "D": 1000, "D+": 1500, "C": 2000, "C+": 2500,
    "B": 3000, "B+": 3500, "A": 4000, "A+": 4500,
    "S": 5000, "S+": 5500, "S++": 6000, "S+++": 6500
}

DEFAULT_MODALITY_CONFIGS = {
    "4v4": {"pr_per_mmr_step": 500, "win_base_pr": 50, "loss_base_pr": 40, "sum_gap_multiplier": 3.0,
            "max_pr_win": 100, "min_pr_win": 15, "max_pr_loss": 50, "min_pr_loss": 10,
            "low_rank_threshold": 2.0, "low_rank_bonus": 10, "high_rank_threshold": 5.0, "high_rank_penalty": 10},
    "2v2": {"pr_per_mmr_step": 500, "win_base_pr": 50, "loss_base_pr": 40, "sum_gap_multiplier": 6.0,
            "max_pr_win": 100, "min_pr_win": 15, "max_pr_loss": 50, "min_pr_loss": 10,
            "low_rank_threshold": 2.0, "low_rank_bonus": 10, "high_rank_threshold": 5.0, "high_rank_penalty": 10},
    "1v1": {"pr_per_mmr_step": 500, "win_base_pr": 50, "loss_base_pr": 40, "sum_gap_multiplier": 10.0,
            "max_pr_win": 100, "min_pr_win": 15, "max_pr_loss": 50, "min_pr_loss": 10,
            "low_rank_threshold": 2.0, "low_rank_bonus": 10, "high_rank_threshold": 5.0, "high_rank_penalty": 10},
}

# ---------------------------------------------------------
# 1. BASE DE DATOS Y ESTRUCTURA
# ---------------------------------------------------------
def init_db():
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    c = conn.cursor()

    # Detect if per-mode columns already exist (migration guard)
    has_mode_cols = True
    try:
        c.execute("SELECT mmr_4v4 FROM players LIMIT 1")
    except sqlite3.OperationalError:
        has_mode_cols = False

    c.execute('''CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    mmr REAL DEFAULT 1000.0,
                    rank_category TEXT DEFAULT 'D',
                    tournament_points REAL DEFAULT 0.0
                )''')
    try:
        c.execute("ALTER TABLE players ADD COLUMN tournament_points REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass

    # Per-mode columns
    for col_name, col_def in [
        ("mmr_4v4", "REAL DEFAULT 1000.0"), ("rank_4v4", "TEXT DEFAULT 'D'"), ("pts_4v4", "REAL DEFAULT 0.0"),
        ("mmr_2v2", "REAL DEFAULT 1000.0"), ("rank_2v2", "TEXT DEFAULT 'D'"), ("pts_2v2", "REAL DEFAULT 0.0"),
        ("mmr_1v1", "REAL DEFAULT 1000.0"), ("rank_1v1", "TEXT DEFAULT 'D'"), ("pts_1v1", "REAL DEFAULT 0.0"),
    ]:
        try:
            c.execute(f"ALTER TABLE players ADD COLUMN {col_name} {col_def}")
        except sqlite3.OperationalError:
            pass

    # Migrate existing player data into per-mode columns
    if not has_mode_cols:
        c.execute("""
            UPDATE players SET
                mmr_4v4=mmr, rank_4v4=rank_category, pts_4v4=tournament_points,
                mmr_2v2=mmr, rank_2v2=rank_category, pts_2v2=tournament_points,
                mmr_1v1=mmr, rank_1v1=rank_category, pts_1v1=tournament_points
        """)

    # Migrate old victory-point pts_* columns to PR thresholds (old values are < 500)
    _rpr_case = ("CASE {rc} WHEN 'D' THEN 1000 WHEN 'D+' THEN 1500 WHEN 'C' THEN 2000 "
                 "WHEN 'C+' THEN 2500 WHEN 'B' THEN 3000 WHEN 'B+' THEN 3500 "
                 "WHEN 'A' THEN 4000 WHEN 'A+' THEN 4500 WHEN 'S' THEN 5000 "
                 "WHEN 'S+' THEN 5500 WHEN 'S++' THEN 6000 ELSE 6500 END")
    for _mode, (_mc, _rc, _pc) in MODE_COLS.items():
        _pr_case = _rpr_case.format(rc=_rc)
        c.execute(f"UPDATE players SET {_pc} = {_pr_case} WHERE {_pc} < 500")
        c.execute(f"UPDATE players SET {_mc} = CAST(CAST({_pc} / 500 AS INTEGER) AS REAL) * 0.5")

    # rank_config: min_points = PR mínimo para ese rango
    c.execute('''CREATE TABLE IF NOT EXISTS rank_config (
                    rank_name TEXT PRIMARY KEY,
                    min_points REAL
                )''')
    c.execute("SELECT COUNT(*) FROM rank_config")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO rank_config (rank_name, min_points) VALUES (?, ?)", [
            ("D", 1000.0), ("D+", 1500.0), ("C", 2000.0), ("C+", 2500.0),
            ("B", 3000.0), ("B+", 3500.0), ("A", 4000.0), ("A+", 4500.0),
            ("S", 5000.0), ("S+", 5500.0), ("S++", 6000.0), ("S+++", 6500.0)
        ])
    else:
        # Migrate rank_config from old victory-point thresholds (< 500) to PR thresholds
        first_val = c.execute("SELECT min_points FROM rank_config ORDER BY min_points DESC LIMIT 1").fetchone()
        if first_val and first_val[0] < 500:
            c.executemany("UPDATE rank_config SET min_points=? WHERE rank_name=?", [
                (1000.0, "D"), (1500.0, "D+"), (2000.0, "C"), (2500.0, "C+"),
                (3000.0, "B"), (3500.0, "B+"), (4000.0, "A"), (4500.0, "A+"),
                (5000.0, "S"), (5500.0, "S+"), (6000.0, "S++"), (6500.0, "S+++")
            ])

    c.execute('''CREATE TABLE IF NOT EXISTS elo_settings (
                    key TEXT PRIMARY KEY,
                    value REAL
                )''')
    c.execute("SELECT COUNT(*) FROM elo_settings")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO elo_settings (key, value) VALUES (?, ?)", [
            ("k_factor", 16.0),          # Sensibilidad Elo conservadora
            ("base_player_mmr", 1000.0), # MMR inicial para nuevos jugadores
            ("scale_factor", 400.0),
            ("auto_promote", 1.0),
            ("win_pts_reward", 1.0)      # 1 punto por victoria (rango sube gradualmente)
        ])
    else:
        # Agregar base_player_mmr si no existe
        try:
            c.execute("INSERT INTO elo_settings (key, value) VALUES ('base_player_mmr', 1000.0)")
        except sqlite3.IntegrityError:
            pass
        # Migrar win_pts_reward si estaba en 3.0 (valor viejo demasiado agresivo)
        old_pts = c.execute("SELECT value FROM elo_settings WHERE key='win_pts_reward'").fetchone()
        if old_pts and old_pts[0] >= 3.0:
            c.execute("UPDATE elo_settings SET value=1.0 WHERE key='win_pts_reward'")
        # Migrar k_factor si era 32 (reducir a 16)
        old_k = c.execute("SELECT value FROM elo_settings WHERE key='k_factor'").fetchone()
        if old_k and old_k[0] >= 32.0:
            c.execute("UPDATE elo_settings SET value=16.0 WHERE key='k_factor'")

    c.execute('''CREATE TABLE IF NOT EXISTS modality_rank_config (
                    modality TEXT PRIMARY KEY,
                    pr_per_mmr_step INTEGER DEFAULT 500,
                    win_base_pr INTEGER DEFAULT 50,
                    loss_base_pr INTEGER DEFAULT 40,
                    sum_gap_multiplier REAL DEFAULT 3.0,
                    max_pr_win INTEGER DEFAULT 100,
                    min_pr_win INTEGER DEFAULT 15,
                    max_pr_loss INTEGER DEFAULT 50,
                    min_pr_loss INTEGER DEFAULT 10,
                    low_rank_threshold REAL DEFAULT 2.0,
                    low_rank_bonus INTEGER DEFAULT 10,
                    high_rank_threshold REAL DEFAULT 5.0,
                    high_rank_penalty INTEGER DEFAULT 10
                )''')
    if c.execute("SELECT COUNT(*) FROM modality_rank_config").fetchone()[0] == 0:
        for _mod, _cfg in DEFAULT_MODALITY_CONFIGS.items():
            c.execute("INSERT INTO modality_rank_config VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_mod, _cfg["pr_per_mmr_step"], _cfg["win_base_pr"], _cfg["loss_base_pr"],
                 _cfg["sum_gap_multiplier"], _cfg["max_pr_win"], _cfg["min_pr_win"],
                 _cfg["max_pr_loss"], _cfg["min_pr_loss"], _cfg["low_rank_threshold"],
                 _cfg["low_rank_bonus"], _cfg["high_rank_threshold"], _cfg["high_rank_penalty"]))

    c.execute('''CREATE TABLE IF NOT EXISTS tournaments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT, created_by TEXT, category TEXT, format TEXT, date TEXT, status TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER, team_name TEXT, total_points REAL
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER, round_name TEXT,
                    team1_id INTEGER, team2_id INTEGER, winner_id INTEGER, status TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_lineups (
                    match_id INTEGER, team_id INTEGER, player_id INTEGER
                )''')

    for _col in [
        "ALTER TABLE matches ADD COLUMN bracket_type TEXT DEFAULT 'UPPER'",
        "ALTER TABLE matches ADD COLUMN round_number INTEGER DEFAULT 1",
        "ALTER TABLE matches ADD COLUMN position INTEGER DEFAULT 0",
        "ALTER TABLE matches ADD COLUMN next_match_winner_id INTEGER",
        "ALTER TABLE matches ADD COLUMN next_match_loser_id INTEGER",
        "ALTER TABLE matches ADD COLUMN is_bracket_reset INTEGER DEFAULT 0",
        "ALTER TABLE matches ADD COLUMN loser_id INTEGER",
        "ALTER TABLE matches ADD COLUMN started_at TEXT",
        "ALTER TABLE matches ADD COLUMN registered_by TEXT",
        "ALTER TABLE matches ADD COLUMN initial_match_snapshot TEXT",
        "ALTER TABLE matches ADD COLUMN final_result_snapshot TEXT",
        "ALTER TABLE matches ADD COLUMN score TEXT",
    ]:
        try: c.execute(_col)
        except sqlite3.OperationalError: pass
    try:
        c.execute("ALTER TABLE tournaments ADD COLUMN bracket_reset_enabled INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_name TEXT, action TEXT, details TEXT, timestamp TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS player_substitutions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    tournament_id INTEGER NOT NULL,
                    team_id INTEGER NOT NULL,
                    player_out_id INTEGER NOT NULL,
                    player_in_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    substituted_at TEXT NOT NULL,
                    player_in_snapshot TEXT NOT NULL
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_rollbacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    tournament_id INTEGER NOT NULL,
                    admin_user TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    executed_at TEXT NOT NULL,
                    previous_snapshot TEXT NOT NULL
                )''')
    conn.commit()
    conn.close()

init_db()

def get_rank_config():
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    df = pd.read_sql("SELECT * FROM rank_config ORDER BY min_points ASC", conn)
    conn.close()
    return dict(zip(df['rank_name'], df['min_points']))

def get_elo_settings():
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    df = pd.read_sql("SELECT * FROM elo_settings", conn)
    conn.close()
    return dict(zip(df['key'], df['value']))

def save_elo_setting(key, val):
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO elo_settings (key, value) VALUES (?, ?)", (key, float(val)))
    conn.commit()
    conn.close()

def get_rank_from_mmr(mmr: float) -> str:
    if mmr <= 1.0: return "D"
    if mmr == 1.5: return "D+"
    if mmr == 2.0: return "C"
    if mmr == 2.5: return "C+"
    if mmr == 3.0: return "B"
    if mmr == 3.5: return "B+"
    if mmr == 4.0: return "A"
    if mmr == 4.5: return "A+"
    if mmr == 5.0: return "S"
    if mmr == 5.5: return "S+"
    if mmr == 6.0: return "S++"
    return "S+++"

def compute_mmr_from_pr(pr: float, pr_per_step: int = 500) -> float:
    return math.floor(pr / pr_per_step) * 0.5

def get_modality_configs() -> dict:
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    try:
        rows = conn.execute("SELECT * FROM modality_rank_config").fetchall()
        cols = [d[0] for d in conn.execute("PRAGMA table_info(modality_rank_config)").fetchall()]
        result = {}
        for row in rows:
            cfg = dict(zip(cols, row))
            result[cfg["modality"]] = cfg
        return result if result else {k: v.copy() for k, v in DEFAULT_MODALITY_CONFIGS.items()}
    except Exception:
        return {k: v.copy() for k, v in DEFAULT_MODALITY_CONFIGS.items()}
    finally:
        conn.close()

def save_modality_config(modality: str, cfg: dict):
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO modality_rank_config
        (modality, pr_per_mmr_step, win_base_pr, loss_base_pr, sum_gap_multiplier,
         max_pr_win, min_pr_win, max_pr_loss, min_pr_loss,
         low_rank_threshold, low_rank_bonus, high_rank_threshold, high_rank_penalty)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (modality, cfg["pr_per_mmr_step"], cfg["win_base_pr"], cfg["loss_base_pr"],
         cfg["sum_gap_multiplier"], cfg["max_pr_win"], cfg["min_pr_win"],
         cfg["max_pr_loss"], cfg["min_pr_loss"], cfg["low_rank_threshold"],
         cfg["low_rank_bonus"], cfg["high_rank_threshold"], cfg["high_rank_penalty"]))
    conn.commit()
    conn.close()

def log_action(admin, action, details):
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    c = conn.cursor()
    c.execute("INSERT INTO audit_logs (admin_name, action, details, timestamp) VALUES (?, ?, ?, ?)",
              (admin, action, details, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# 2. MOTOR DE DRAFT: AUDITORÍA Y TIERED POOL
# ---------------------------------------------------------
def audit_team_balance(teams_data, player_dict):
    """
    Check draft balance using simplified MMR (per requerimientos2.txt).
    player_dict[name] = (id, name, mode_mmr, mode_rank)
    Returns (is_balanced, deviations, target, swap_suggestion)
    """
    team_sums = {}
    for t_name, plist in teams_data.items():
        s = sum(RANK_MMR_SIMPLE.get(player_dict[p][3], 1.0) for p in plist if p in player_dict)
        team_sums[t_name] = (s, plist)

    total = sum(v[0] for v in team_sums.values())
    n = len(team_sums)
    target = total / n if n > 0 else 0.0

    deviations = {t: abs(v[0] - target) for t, v in team_sums.items()}
    is_balanced = all(d <= BALANCE_TOLERANCE for d in deviations.values())

    swap_suggestion = None
    if not is_balanced:
        max_t = max(team_sums, key=lambda t: team_sums[t][0])
        min_t = min(team_sums, key=lambda t: team_sums[t][0])
        delta = team_sums[max_t][0] - team_sums[min_t][0]
        max_cap = team_sums[max_t][1][0]
        min_cap = team_sums[min_t][1][0]

        best_diff = float('inf')
        best = None
        for pa in team_sums[max_t][1]:
            if pa == max_cap or pa not in player_dict: continue
            for pb in team_sums[min_t][1]:
                if pb == min_cap or pb not in player_dict: continue
                ma = RANK_MMR_SIMPLE.get(player_dict[pa][3], 1.0)
                mb = RANK_MMR_SIMPLE.get(player_dict[pb][3], 1.0)
                diff = abs((ma - mb) - delta / 2)
                if diff < best_diff:
                    best_diff = diff
                    best = (pa, pb, max_t, min_t, ma, mb)

        if best:
            pa, pb, ta, tb, ma, mb = best
            new_a = round(team_sums[ta][0] - ma + mb, 2)
            new_b = round(team_sums[tb][0] - mb + ma, 2)
            swap_suggestion = {
                "player_from": pa, "team_from": ta, "rank_from": player_dict[pa][3], "mmr_from": ma,
                "player_to":   pb, "team_to":   tb, "rank_to":   player_dict[pb][3], "mmr_to":   mb,
                "new_sum_from": new_a, "new_sum_to": new_b, "target": round(target, 2),
            }

    return is_balanced, deviations, round(target, 2), swap_suggestion


def tiered_pool_draft(players_sorted, calc_teams, players_per_team):
    """
    Divide N players into M pools (tiers); each team gets one player from each tier.
    Returns list of teams (each team = list of player names).
    """
    N = calc_teams * players_per_team
    pool_players = list(players_sorted[:N])
    pools = []
    for i in range(players_per_team):
        pool = pool_players[i * calc_teams:(i + 1) * calc_teams]
        random.shuffle(pool)
        pools.append(pool)
    return [[pools[tier][team_idx] for tier in range(players_per_team)] for team_idx in range(calc_teams)]


# ---------------------------------------------------------
# 3. MOTOR DE CÁLCULO (MMR + PUNTOS POR VICTORIA)
# ---------------------------------------------------------
def _get_game_mode_for_match(c, match_id):
    row = c.execute(
        "SELECT t.category FROM matches m JOIN tournaments t ON m.tournament_id=t.id WHERE m.id=?",
        (match_id,)).fetchone()
    return row[0] if row else "4v4"


def _rank_for_pts(pts, ranks_desc):
    """Determina el rango según puntos de victoria acumulados (no MMR)."""
    for rn, rp in ranks_desc:
        if pts >= rp:
            return rn
    return ranks_desc[-1][0] if ranks_desc else "D"


def process_match_victory(winning_team_id, losing_team_id, match_id):
    mod_configs = get_modality_configs()
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    c = conn.cursor()

    game_mode = _get_game_mode_for_match(c, match_id)
    mc, rc, pc = MODE_COLS.get(game_mode, MODE_COLS["4v4"])
    cfg = mod_configs.get(game_mode, DEFAULT_MODALITY_CONFIGS.get(game_mode, DEFAULT_MODALITY_CONFIGS["4v4"]))

    win_pl  = c.execute(f"SELECT DISTINCT p.id,p.{mc},p.{pc} FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.team_id=?", (winning_team_id,)).fetchall()
    loss_pl = c.execute(f"SELECT DISTINCT p.id,p.{mc},p.{pc} FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.team_id=?", (losing_team_id,)).fetchall()

    if not win_pl or not loss_pl:
        conn.close()
        return 0

    win_team_sum  = sum(p[1] for p in win_pl)
    loss_team_sum = sum(p[1] for p in loss_pl)
    gap = win_team_sum - loss_team_sum

    win_base_calc  = cfg["win_base_pr"]  - (gap * cfg["sum_gap_multiplier"])
    loss_base_calc = cfg["loss_base_pr"] + (gap * cfg["sum_gap_multiplier"])
    pr_win_base  = max(cfg["min_pr_win"],  min(cfg["max_pr_win"],  win_base_calc))
    pr_loss_base = max(cfg["min_pr_loss"], min(cfg["max_pr_loss"], loss_base_calc))

    pr_delta_display = 0
    for pid, curr_mmr, curr_pr in win_pl:
        pr_change = pr_win_base
        if curr_mmr <= cfg["low_rank_threshold"]:
            pr_change += cfg["low_rank_bonus"]
        elif curr_mmr >= cfg["high_rank_threshold"]:
            pr_change -= cfg["high_rank_penalty"]
        pr_delta = round(pr_change)
        new_pr   = max(0, curr_pr + pr_delta)
        new_mmr  = compute_mmr_from_pr(new_pr, int(cfg["pr_per_mmr_step"]))
        new_rank = get_rank_from_mmr(new_mmr)
        c.execute(f"UPDATE players SET {mc}=?,{rc}=?,{pc}=? WHERE id=?", (new_mmr, new_rank, new_pr, pid))
        pr_delta_display = pr_delta

    for pid, curr_mmr, curr_pr in loss_pl:
        pr_delta = -round(pr_loss_base)
        new_pr   = max(0, curr_pr + pr_delta)
        new_mmr  = compute_mmr_from_pr(new_pr, int(cfg["pr_per_mmr_step"]))
        new_rank = get_rank_from_mmr(new_mmr)
        c.execute(f"UPDATE players SET {mc}=?,{rc}=?,{pc}=? WHERE id=?", (new_mmr, new_rank, new_pr, pid))

    c.execute("UPDATE matches SET winner_id=?,status='Completed' WHERE id=?", (winning_team_id, match_id))

    t_id = c.execute("SELECT tournament_id FROM matches WHERE id=?", (match_id,)).fetchone()[0]
    frm  = c.execute("SELECT id,status,winner_id FROM matches WHERE tournament_id=? AND round_name='Primera Ronda'", (t_id,)).fetchall()
    if len(frm) >= 2 and all(r[1] == 'Completed' for r in frm):
        if c.execute("SELECT COUNT(*) FROM matches WHERE tournament_id=? AND round_name='GRAN FINAL'", (t_id,)).fetchone()[0] == 0:
            w1, w2 = frm[0][2], frm[1][2]
            c.execute("INSERT INTO matches (tournament_id,round_name,team1_id,team2_id,status) VALUES (?,?,?,?,?)",
                      (t_id, "GRAN FINAL", w1, w2, "Pending"))
            fm = c.lastrowid
            for tw in [w1, w2]:
                for (pl,) in c.execute("SELECT DISTINCT player_id FROM match_lineups WHERE team_id=?", (tw,)).fetchall():
                    c.execute("INSERT INTO match_lineups (match_id,team_id,player_id) VALUES (?,?,?)", (fm, tw, pl))

    conn.commit(); conn.close()
    return pr_delta_display


# ---------------------------------------------------------
# 4. MOTOR DE BRACKETS (Eliminación Simple y Doble)
# ---------------------------------------------------------
def _bracket_size(n):
    if n < 2: return 2
    return 2 ** math.ceil(math.log2(n))

def _seeded_slots(size):
    s = [1, 2]
    while len(s) < size:
        new_s = []
        n = len(s) * 2
        for x in s:
            new_s.append(x)
            new_s.append(n + 1 - x)
        s = new_s
    return s

def _fill_slot(c, match_id, team_id):
    m = c.execute("SELECT team1_id,team2_id,status FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m: return
    if m[0] is None: c.execute("UPDATE matches SET team1_id=? WHERE id=?", (team_id, match_id))
    else:            c.execute("UPDATE matches SET team2_id=? WHERE id=?", (team_id, match_id))
    m2 = c.execute("SELECT team1_id,team2_id,status FROM matches WHERE id=?", (match_id,)).fetchone()
    if m2[0] and m2[1] and m2[2] == 'PENDING':
        c.execute("UPDATE matches SET status='READY' WHERE id=?", (match_id,))

def _copy_lineup(c, from_match, team_id, to_match):
    rows = c.execute("SELECT DISTINCT player_id FROM match_lineups WHERE match_id=? AND team_id=?", (from_match, team_id)).fetchall()
    for (pl,) in rows:
        c.execute("""INSERT INTO match_lineups (match_id,team_id,player_id)
            SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM match_lineups WHERE match_id=? AND team_id=? AND player_id=?)""",
            (to_match, team_id, pl, to_match, team_id, pl))

def _auto_bye(c, match_id, team_id):
    c.execute("UPDATE matches SET winner_id=?,loser_id=NULL,status='BYE_COMPLETED' WHERE id=?", (team_id, match_id))
    nxt = c.execute("SELECT next_match_winner_id FROM matches WHERE id=?", (match_id,)).fetchone()
    if nxt and nxt[0]:
        _fill_slot(c, nxt[0], team_id)
        _copy_lineup(c, match_id, team_id, nxt[0])

def _propagate_lb_byes(c, t_id):
    changed = True
    while changed:
        changed = False
        for m_id, t1, t2 in c.execute(
            "SELECT id,team1_id,team2_id FROM matches WHERE tournament_id=? AND bracket_type='LOWER' AND status='PENDING'", (t_id,)).fetchall():
            ub_p = c.execute("SELECT COUNT(*) FROM matches WHERE next_match_loser_id=? AND tournament_id=? AND status NOT IN ('BYE_COMPLETED','COMPLETED')", (m_id, t_id)).fetchone()[0]
            lb_p = c.execute("SELECT COUNT(*) FROM matches WHERE next_match_winner_id=? AND tournament_id=? AND bracket_type='LOWER' AND status NOT IN ('BYE_COMPLETED','COMPLETED')", (m_id, t_id)).fetchone()[0]
            if (ub_p + lb_p) > 0: continue
            if   t1 and not t2: _auto_bye(c, m_id, t1); changed = True
            elif t2 and not t1: _auto_bye(c, m_id, t2); changed = True
            elif not t1 and not t2: c.execute("UPDATE matches SET status='BYE_COMPLETED' WHERE id=?", (m_id,)); changed = True

def _create_ub(c, t_id, B, N):
    ub = {}
    def rn(r):
        if r == N: return "Gran Final" if N == 1 else "Final (UB)"
        if r == N-1 and N > 2: return "Semifinal (UB)"
        return f"Ronda {r} (UB)"
    for r in range(1, N+1):
        ub[r] = []
        for pos in range(B // (2**r)):
            c.execute("INSERT INTO matches (tournament_id,round_name,bracket_type,round_number,position,status) VALUES (?,?,'UPPER',?,?,'PENDING')", (t_id, rn(r), r, pos))
            ub[r].append(c.lastrowid)
    for r in range(1, N):
        for pos, m in enumerate(ub[r]):
            c.execute("UPDATE matches SET next_match_winner_id=? WHERE id=?", (ub[r+1][pos//2], m))
    return ub

def _create_lb(c, t_id, B, N):
    lb = {}
    total = 2*(N-1)
    def rn(lr):
        if lr == total: return "Final (LB)"
        return f"Ronda {lr} (LB)"
    for k in range(1, N):
        n_m = B // (2**(k+1))
        for lr in [2*k-1, 2*k]:
            lb[lr] = []
            for pos in range(n_m):
                c.execute("INSERT INTO matches (tournament_id,round_name,bracket_type,round_number,position,status) VALUES (?,?,'LOWER',?,?,'PENDING')", (t_id, rn(lr), lr, pos))
                lb[lr].append(c.lastrowid)
    for lr in range(1, total):
        for pos, m in enumerate(lb[lr]):
            c.execute("UPDATE matches SET next_match_winner_id=? WHERE id=?", (lb[lr+1][pos//2], m))
    return lb

def _link_lb(c, ub, lb, N):
    total_lb = 2*(N-1)
    for ub_r, ub_list in ub.items():
        if ub_r == 1:   lr = 1
        elif ub_r == N: lr = total_lb
        else:           lr = 2*(ub_r-1)
        lb_list = lb.get(lr, [])
        n_ub, n_lb = len(ub_list), len(lb_list)
        for pos, m in enumerate(ub_list):
            if n_lb:
                lb_pos = (n_ub-1-pos) % n_lb if n_lb > 1 else 0
                c.execute("UPDATE matches SET next_match_loser_id=? WHERE id=?", (lb_list[lb_pos], m))

def create_bracket_single(t_id, seed_team_ids, conn):
    c = conn.cursor()
    n = len(seed_team_ids); B = _bracket_size(n); N = int(math.log2(B))
    seed = {i+1: (seed_team_ids[i] if i < n else None) for i in range(B)}
    slots = _seeded_slots(B); ub = _create_ub(c, t_id, B, N)
    for i, m in enumerate(ub[1]):
        ta, tb = seed.get(slots[i*2]), seed.get(slots[i*2+1])
        if ta and tb: c.execute("UPDATE matches SET team1_id=?,team2_id=?,status='READY' WHERE id=?", (ta,tb,m))
        elif ta:      c.execute("UPDATE matches SET team1_id=? WHERE id=?", (ta,m)); _auto_bye(c,m,ta)
        elif tb:      c.execute("UPDATE matches SET team2_id=? WHERE id=?", (tb,m)); _auto_bye(c,m,tb)
    return ub

def create_bracket_double(t_id, seed_team_ids, bracket_reset, conn):
    c = conn.cursor()
    n = len(seed_team_ids); B = _bracket_size(n); N = int(math.log2(B))
    seed = {i+1: (seed_team_ids[i] if i < n else None) for i in range(B)}
    slots = _seeded_slots(B)
    ub = _create_ub(c, t_id, B, N); lb = _create_lb(c, t_id, B, N); _link_lb(c, ub, lb, N)
    c.execute("INSERT INTO matches (tournament_id,round_name,bracket_type,round_number,position,status) VALUES (?,'Gran Final','GRAND_FINAL',1,0,'PENDING')", (t_id,))
    gf = c.lastrowid
    total_lb = 2*(N-1)
    c.execute("UPDATE matches SET next_match_winner_id=? WHERE id=?", (gf, ub[N][0]))
    c.execute("UPDATE matches SET next_match_winner_id=? WHERE id=?", (gf, lb[total_lb][0]))
    if bracket_reset:
        c.execute("INSERT INTO matches (tournament_id,round_name,bracket_type,round_number,position,is_bracket_reset,status) VALUES (?,'Gran Final - Reset','GRAND_FINAL',2,0,1,'PENDING')", (t_id,))
    for i, m in enumerate(ub[1]):
        ta, tb = seed.get(slots[i*2]), seed.get(slots[i*2+1])
        if ta and tb: c.execute("UPDATE matches SET team1_id=?,team2_id=?,status='READY' WHERE id=?", (ta,tb,m))
        elif ta:      c.execute("UPDATE matches SET team1_id=? WHERE id=?", (ta,m)); _auto_bye(c,m,ta)
        elif tb:      c.execute("UPDATE matches SET team2_id=? WHERE id=?", (tb,m)); _auto_bye(c,m,tb)
    _propagate_lb_byes(c, t_id)
    return ub, lb, gf

def process_match_result(match_id, winning_team_id, losing_team_id, registered_by='', score=''):
    mod_configs = get_modality_configs()
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    c = conn.cursor()

    game_mode = _get_game_mode_for_match(c, match_id)
    mc, rc, pc = MODE_COLS.get(game_mode, MODE_COLS["4v4"])
    cfg = mod_configs.get(game_mode, DEFAULT_MODALITY_CONFIGS.get(game_mode, DEFAULT_MODALITY_CONFIGS["4v4"]))

    win_pl  = c.execute(f"SELECT DISTINCT p.id, p.{mc}, p.{pc}, p.{rc}, p.name FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (match_id, winning_team_id)).fetchall()
    loss_pl = c.execute(f"SELECT DISTINCT p.id, p.{mc}, p.{pc}, p.{rc}, p.name FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (match_id, losing_team_id)).fetchall()

    pr_delta_display = 0
    player_results = []
    if win_pl and loss_pl:
        win_team_sum  = sum(p[1] for p in win_pl)
        loss_team_sum = sum(p[1] for p in loss_pl)
        gap = win_team_sum - loss_team_sum

        win_base_calc  = cfg["win_base_pr"]  - (gap * cfg["sum_gap_multiplier"])
        loss_base_calc = cfg["loss_base_pr"] + (gap * cfg["sum_gap_multiplier"])
        pr_win_base  = max(cfg["min_pr_win"],  min(cfg["max_pr_win"],  win_base_calc))
        pr_loss_base = max(cfg["min_pr_loss"], min(cfg["max_pr_loss"], loss_base_calc))

        for pid, curr_mmr, curr_pr, curr_rank, p_name in win_pl:
            pr_change = pr_win_base
            bonuses = ["ExpectedWinBase"]
            if curr_mmr <= cfg["low_rank_threshold"]:
                pr_change += cfg["low_rank_bonus"]
                bonuses.append("LowRankBonus")
            elif curr_mmr >= cfg["high_rank_threshold"]:
                pr_change -= cfg["high_rank_penalty"]
                bonuses.append("HighRankPenalty")
            pr_delta = round(pr_change)
            new_pr   = max(0, curr_pr + pr_delta)
            new_mmr  = compute_mmr_from_pr(new_pr, int(cfg["pr_per_mmr_step"]))
            new_rank = get_rank_from_mmr(new_mmr)
            c.execute(f"UPDATE players SET {mc}=?, {rc}=?, {pc}=? WHERE id=?", (new_mmr, new_rank, new_pr, pid))
            pr_delta_display = pr_delta
            player_results.append({
                "playerId": str(pid), "gamertag": p_name, "result": "WIN",
                "prBefore": curr_pr, "prChange": pr_delta, "prAfter": new_pr,
                "mmrBefore": round(curr_mmr, 2), "mmrAfter": round(new_mmr, 2),
                "rankBefore": curr_rank, "rankAfter": new_rank, "appliedBonuses": bonuses
            })

        for pid, curr_mmr, curr_pr, curr_rank, p_name in loss_pl:
            pr_delta = -round(pr_loss_base)
            bonuses = ["ExpectedLossBase"]
            new_pr   = max(0, curr_pr + pr_delta)
            new_mmr  = compute_mmr_from_pr(new_pr, int(cfg["pr_per_mmr_step"]))
            new_rank = get_rank_from_mmr(new_mmr)
            c.execute(f"UPDATE players SET {mc}=?, {rc}=?, {pc}=? WHERE id=?", (new_mmr, new_rank, new_pr, pid))
            player_results.append({
                "playerId": str(pid), "gamertag": p_name, "result": "LOSS",
                "prBefore": curr_pr, "prChange": pr_delta, "prAfter": new_pr,
                "mmrBefore": round(curr_mmr, 2), "mmrAfter": round(new_mmr, 2),
                "rankBefore": curr_rank, "rankAfter": new_rank, "appliedBonuses": bonuses
            })

    final_snapshot = json.dumps({
        "matchId": str(match_id),
        "completedAt": datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "winnerTeamId": str(winning_team_id),
        "score": score or "—",
        "registeredBy": registered_by or "Sistema",
        "playerResults": player_results
    })
    c.execute("UPDATE matches SET winner_id=?, loser_id=?, status='COMPLETED', registered_by=?, score=?, final_result_snapshot=? WHERE id=?",
              (winning_team_id, losing_team_id, registered_by, score, final_snapshot, match_id))
    mi = c.execute("SELECT bracket_type, next_match_winner_id, next_match_loser_id, is_bracket_reset, tournament_id FROM matches WHERE id=?", (match_id,)).fetchone()
    if not mi:
        conn.commit(); conn.close()
        return pr_delta_display
    btype, nxt_w, nxt_l, is_reset, t_id = mi
    ti = c.execute("SELECT format, bracket_reset_enabled FROM tournaments WHERE id=?", (t_id,)).fetchone()
    t_fmt = ti[0] if ti else "Eliminación Directa"
    br_en = bool(ti[1]) if ti else True
    if nxt_w: _fill_slot(c, nxt_w, winning_team_id); _copy_lineup(c, match_id, winning_team_id, nxt_w)
    if t_fmt == "Doble Eliminación" and btype == 'UPPER' and nxt_l:
        _fill_slot(c, nxt_l, losing_team_id); _copy_lineup(c, match_id, losing_team_id, nxt_l)
    if t_fmt == "Doble Eliminación": _propagate_lb_byes(c, t_id)
    if btype == 'GRAND_FINAL' and not is_reset:
        ub_winner = c.execute("SELECT winner_id FROM matches WHERE tournament_id=? AND bracket_type='UPPER' ORDER BY round_number DESC LIMIT 1", (t_id,)).fetchone()
        lb_won = ub_winner and ub_winner[0] != winning_team_id
        if lb_won and br_en:
            rm = c.execute("SELECT id FROM matches WHERE tournament_id=? AND is_bracket_reset=1", (t_id,)).fetchone()
            if rm:
                c.execute("UPDATE matches SET team1_id=?, team2_id=?, status='READY' WHERE id=?", (winning_team_id, losing_team_id, rm[0]))
                _copy_lineup(c, match_id, winning_team_id, rm[0]); _copy_lineup(c, match_id, losing_team_id, rm[0])
    conn.commit(); conn.close()
    return pr_delta_display


def start_match(match_id, judge_name, t_cat="4v4"):
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    c = conn.cursor()
    mc, rc, pc = MODE_COLS.get(t_cat, MODE_COLS["4v4"])
    m = c.execute("SELECT team1_id, team2_id, tournament_id FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m:
        conn.close(); return
    t1_id, t2_id, t_id = m
    t1_info = c.execute("SELECT team_name FROM teams WHERE id=?", (t1_id,)).fetchone()
    t2_info = c.execute("SELECT team_name FROM teams WHERE id=?", (t2_id,)).fetchone()

    def _roster(team_id):
        rows = c.execute(
            f"SELECT DISTINCT p.id, p.name, p.{mc}, p.{pc}, p.{rc} FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?",
            (match_id, team_id)).fetchall()
        return [{"playerId": str(r[0]), "gamertag": r[1], "mmrBefore": round(r[2], 2),
                 "prBefore": int(r[3]), "rankBefore": r[4], "isRinger": False} for r in rows], \
               sum(r[2] for r in rows)

    roster_a, mmr_a = _roster(t1_id)
    roster_b, mmr_b = _roster(t2_id)
    snapshot = json.dumps({
        "matchId": str(match_id),
        "startedAt": datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "judgeBy": judge_name,
        "teams": {
            "teamA": {"teamId": str(t1_id), "name": t1_info[0] if t1_info else "Equipo A",
                      "totalMMR": round(mmr_a, 2), "roster": roster_a},
            "teamB": {"teamId": str(t2_id), "name": t2_info[0] if t2_info else "Equipo B",
                      "totalMMR": round(mmr_b, 2), "roster": roster_b}
        }
    })
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("UPDATE matches SET status='IN_PROGRESS', started_at=?, initial_match_snapshot=?, registered_by=? WHERE id=?",
              (now_str, snapshot, judge_name, match_id))
    conn.commit(); conn.close()


# ---------------------------------------------------------
# BRACKET SVG RENDERER
# ---------------------------------------------------------
def _render_bracket_svg(bmatches, accent="#0284c7"):
    """
    Renders a tournament bracket as scrollable HTML + SVG connector lines.
    bmatches: tuples with indices 9=round_number, 10=position.
    """
    from collections import defaultdict
    # Hide the pre-created reset slot if it was never triggered
    bmatches = [m for m in bmatches if not (m[8] == 1 and m[4] == 'PENDING')]
    if not bmatches:
        return ""

    rmap = defaultdict(list)
    for m in bmatches:
        rmap[m[9]].append(m)

    rnums = sorted(rmap.keys())
    nr    = len(rnums)
    for rn in rnums:
        rmap[rn].sort(key=lambda x: x[10])

    r1n = len(rmap[rnums[0]])

    CW, CH, TH = 188, 80, 38
    CON = 34
    SH  = CH + 20
    HDR = 36
    PX, PY = 16, 10

    total_h = PY + HDR + r1n * SH + PY
    total_w = PX + nr * CW + (nr - 1) * CON + PX

    SVG, CARDS = [], []

    _SS = {
        "win":  ("#f0fdf4", "#15803d", "700", ""),
        "lose": ("#f8fafc", "#94a3b8", "400", "text-decoration:line-through;opacity:0.55;"),
        "hot":  ("#fff7ed", "#c2410c", "700", ""),
        "wait": ("#f8fafc", "#475569", "500", ""),
    }

    def _rlabel(ri):
        fe = nr - 1 - ri
        nm = len(rmap[rnums[ri]])
        if fe == 0 and nm == 1: return "GRAN FINAL"
        if fe == 1:             return "SEMIFINAL"
        if fe == 2:             return "CUARTOS"
        if ri == 0:             return "PRIMERA RONDA"
        return f"RONDA {ri+1}"

    def _tslot(name, state, is_win):
        bg, tc, fw, ex = _SS[state]
        trophy = "🏆&nbsp;" if is_win else ""
        dot = (f"<span style='width:6px;height:6px;border-radius:50%;display:inline-block;"
               f"margin-right:5px;background:#f97316;flex-shrink:0;'></span>") if state == "hot" else ""
        return (f"<div style='background:{bg};height:{TH}px;display:flex;align-items:center;"
                f"padding:0 10px;overflow:hidden;'>{dot}"
                f"<span style='font-size:0.79rem;font-weight:{fw};color:{tc};{ex}"
                f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{trophy}{name}</span>"
                f"</div>")

    for ri, rnum in enumerate(rnums):
        ms   = rmap[rnum]
        nm   = len(ms)
        spm  = max(1, r1n // nm)
        cx   = PX + ri * (CW + CON)
        nm1  = len(rmap[rnums[ri + 1]]) if ri < nr - 1 else 0

        CARDS.append(
            f"<div style='position:absolute;left:{cx}px;top:{PY}px;width:{CW}px;"
            f"height:{HDR}px;display:flex;align-items:center;justify-content:center;'>"
            f"<span style='font-size:0.58rem;font-weight:800;letter-spacing:0.13em;"
            f"text-transform:uppercase;color:#94a3b8;'>{_rlabel(ri)}</span></div>"
        )

        for mi, m in enumerate(ms):
            m_id, _, t1n, t2n, sv, t1i, t2i, wid, _isr, _, _ = m
            cy   = PY + HDR + mi * spm * SH + (spm * SH - CH) // 2
            done = sv in ('COMPLETED', 'BYE_COMPLETED')
            live = sv == 'IN_PROGRESS'
            rdy  = sv == 'READY'

            t1d = t1n or "···"
            t2d = t2n or "···"
            if done:
                s1 = "win" if wid == t1i else "lose"
                s2 = "win" if wid == t2i else "lose"
            elif live or rdy:
                s1 = s2 = "hot"
            else:
                s1 = s2 = "wait"

            top_c = "#22c55e" if done else ("#f97316" if live else (accent if rdy else "#e2e8f0"))
            CARDS.append(
                f"<div style='position:absolute;left:{cx}px;top:{cy}px;width:{CW}px;height:{CH}px;"
                f"background:#fff;border:1.5px solid #e2e8f0;border-top:3px solid {top_c};"
                f"border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.07);'>"
                + _tslot(t1d, s1, done and wid == t1i)
                + "<div style='height:1px;background:#f1f5f9;'></div>"
                + _tslot(t2d, s2, done and wid == t2i)
                + "</div>"
            )

            if ri < nr - 1 and nm1 > 0:
                rx  = cx + CW
                ry  = cy + CH // 2
                mid = rx + CON // 2
                spm1 = max(1, r1n // nm1)
                lc   = "#cbd5e1"
                tx   = cx + CW + CON
                if nm1 == nm // 2 or (nm1 < nm and nm % nm1 == 0):
                    nmi = mi // 2
                    ncy = PY + HDR + nmi * spm1 * SH + (spm1 * SH - CH) // 2
                    ty  = ncy + CH // 2
                    SVG.append(f"<line x1='{rx}' y1='{ry}' x2='{mid}' y2='{ry}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                    if mi % 2 == 0:
                        # V line goes from this match center all the way to the BOTTOM match center
                        ry_bot = PY + HDR + (mi + 1) * spm * SH + (spm * SH - CH) // 2 + CH // 2
                        SVG.append(f"<line x1='{mid}' y1='{ry}' x2='{mid}' y2='{ry_bot}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                        SVG.append(f"<line x1='{mid}' y1='{ty}' x2='{tx}' y2='{ty}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                else:
                    nmi = min(mi, nm1 - 1)
                    ncy = PY + HDR + nmi * spm1 * SH + (spm1 * SH - CH) // 2
                    ty  = ncy + CH // 2
                    SVG.append(f"<line x1='{rx}' y1='{ry}' x2='{mid}' y2='{ry}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                    SVG.append(f"<line x1='{mid}' y1='{ry}' x2='{mid}' y2='{ty}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                    SVG.append(f"<line x1='{mid}' y1='{ty}' x2='{tx}' y2='{ty}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")

    svg_el = (f"<svg style='position:absolute;top:0;left:0;width:{total_w}px;height:{total_h}px;"
              f"pointer-events:none;overflow:visible;'>" + "".join(SVG) + "</svg>")
    return (f"<div style='overflow-x:auto;background:#f8fafc;"
            f"border:1.5px solid #e2e8f0;border-radius:12px;"
            f"padding:6px 6px 14px 6px;margin-bottom:20px;'>"
            f"<div style='position:relative;width:{total_w}px;height:{total_h}px;overflow:visible;'>"
            + svg_el + "".join(CARDS) + "</div></div>")


def _build_combined_svg(sections):
    """
    sections: list of (label, accent, bmatches)
    Returns a standalone SVG string with all brackets stacked vertically.
    """
    from collections import defaultdict
    # Strip unactivated reset slots from every section
    sections = [(lbl, acc, [m for m in bm if not (m[8] == 1 and m[4] == 'PENDING')])
                for lbl, acc, bm in sections]
    sections = [(lbl, acc, bm) for lbl, acc, bm in sections if bm]

    CW, CH, TH = 188, 80, 38
    CON = 34
    SH  = CH + 20
    HDR = 36
    PX, PY = 20, 12
    SEC_GAP   = 48
    SEC_LBL_H = 32
    FONT = "system-ui,-apple-system,sans-serif"

    def _xe(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    _SS = {
        "win":  ("#f0fdf4", "#15803d", "700"),
        "lose": ("#f8fafc", "#94a3b8", "400"),
        "hot":  ("#fff7ed", "#c2410c", "600"),
        "wait": ("#f8fafc", "#475569", "500"),
    }

    def _rlabel(ri, nr, rmap, rnums):
        fe = nr - 1 - ri
        nm = len(rmap[rnums[ri]])
        if fe == 0 and nm == 1: return "GRAN FINAL"
        if fe == 1:             return "SEMIFINAL"
        if fe == 2:             return "CUARTOS"
        if ri == 0:             return "PRIMERA RONDA"
        return f"RONDA {ri+1}"

    elems = []
    cur_y = PY
    total_w = 0

    for label, accent, bmatches in sections:
        if not bmatches:
            continue
        rmap = defaultdict(list)
        for m in bmatches:
            rmap[m[9]].append(m)
        rnums = sorted(rmap.keys())
        nr    = len(rnums)
        for rn in rnums:
            rmap[rn].sort(key=lambda x: x[10])
        r1n   = len(rmap[rnums[0]])
        sec_w = PX + nr * CW + (nr - 1) * CON + PX
        total_w = max(total_w, sec_w)

        # Section label bar
        elems.append(f"<rect x='{PX}' y='{cur_y+4}' width='4' height='{SEC_LBL_H-10}' rx='2' fill='{accent}'/>")
        elems.append(
            f"<text x='{PX+12}' y='{cur_y + SEC_LBL_H//2 + 5}' font-family='{FONT}' "
            f"font-size='10' font-weight='800' fill='{accent}' letter-spacing='1.8'>"
            f"{_xe(label.upper())}</text>"
        )

        by = cur_y + SEC_LBL_H  # y offset where the bracket grid starts

        for ri, rnum in enumerate(rnums):
            ms   = rmap[rnum]
            nm   = len(ms)
            spm  = max(1, r1n // nm)
            cx   = PX + ri * (CW + CON)
            nm1  = len(rmap[rnums[ri + 1]]) if ri < nr - 1 else 0

            # Round column header
            rl = _xe(_rlabel(ri, nr, rmap, rnums))
            elems.append(
                f"<text x='{cx + CW//2}' y='{by + PY + HDR//2 + 4}' text-anchor='middle' "
                f"font-family='{FONT}' font-size='8' font-weight='800' fill='#94a3b8' letter-spacing='1.5'>"
                f"{rl}</text>"
            )

            for mi, m in enumerate(ms):
                m_id, _, t1n, t2n, sv, t1i, t2i, wid, _isr, _, _ = m
                cy   = by + PY + HDR + mi * spm * SH + (spm * SH - CH) // 2
                done = sv in ('COMPLETED', 'BYE_COMPLETED')
                live = sv == 'IN_PROGRESS'
                rdy  = sv == 'READY'

                t1d = _xe((t1n or "···")[:24])
                t2d = _xe((t2n or "···")[:24])

                s1 = ("win" if wid == t1i else "lose") if done else ("hot" if (live or rdy) else "wait")
                s2 = ("win" if wid == t2i else "lose") if done else ("hot" if (live or rdy) else "wait")

                top_c = "#22c55e" if done else ("#f97316" if live else (accent if rdy else "#e2e8f0"))
                bg1, tc1, fw1 = _SS[s1]
                bg2, tc2, fw2 = _SS[s2]

                # Card outline
                elems.append(
                    f"<rect x='{cx}' y='{cy}' width='{CW}' height='{CH}' rx='6' ry='6' "
                    f"fill='#ffffff' stroke='#e2e8f0' stroke-width='1.5'/>"
                )
                # Colored top border
                elems.append(
                    f"<rect x='{cx}' y='{cy}' width='{CW}' height='3' rx='0' fill='{top_c}'/>"
                )
                # Slot 1 background
                elems.append(
                    f"<rect x='{cx+1}' y='{cy+3}' width='{CW-2}' height='{TH-3}' fill='{bg1}'/>"
                )
                # Slot 1 text
                win1 = "★ " if (done and wid == t1i) else ""
                elems.append(
                    f"<text x='{cx+8}' y='{cy+3+(TH-3)//2+4}' font-family='{FONT}' "
                    f"font-size='11' font-weight='{fw1}' fill='{tc1}'>{win1}{t1d}</text>"
                )
                # Divider
                elems.append(
                    f"<line x1='{cx+1}' y1='{cy+TH}' x2='{cx+CW-1}' y2='{cy+TH}' "
                    f"stroke='#f1f5f9' stroke-width='1'/>"
                )
                # Slot 2 background
                elems.append(
                    f"<rect x='{cx+1}' y='{cy+TH}' width='{CW-2}' height='{CH-TH-1}' fill='{bg2}'/>"
                )
                # Slot 2 text
                win2 = "★ " if (done and wid == t2i) else ""
                elems.append(
                    f"<text x='{cx+8}' y='{cy+TH+(CH-TH)//2+4}' font-family='{FONT}' "
                    f"font-size='11' font-weight='{fw2}' fill='{tc2}'>{win2}{t2d}</text>"
                )

                # Connector lines to next round
                if ri < nr - 1 and nm1 > 0:
                    rx_ = cx + CW
                    ry_ = cy + CH // 2
                    mid = rx_ + CON // 2
                    spm1 = max(1, r1n // nm1)
                    lc = "#cbd5e1"
                    tx_ = cx + CW + CON
                    if nm1 == nm // 2 or (nm1 < nm and nm % nm1 == 0):
                        nmi  = mi // 2
                        ncy  = by + PY + HDR + nmi * spm1 * SH + (spm1 * SH - CH) // 2
                        ty_  = ncy + CH // 2
                        elems.append(f"<line x1='{rx_}' y1='{ry_}' x2='{mid}' y2='{ry_}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                        if mi % 2 == 0:
                            ry_bot = by + PY + HDR + (mi + 1) * spm * SH + (spm * SH - CH) // 2 + CH // 2
                            elems.append(f"<line x1='{mid}' y1='{ry_}' x2='{mid}' y2='{ry_bot}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                            elems.append(f"<line x1='{mid}' y1='{ty_}' x2='{tx_}' y2='{ty_}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                    else:
                        nmi = min(mi, nm1 - 1)
                        ncy = by + PY + HDR + nmi * spm1 * SH + (spm1 * SH - CH) // 2
                        ty_ = ncy + CH // 2
                        elems.append(f"<line x1='{rx_}' y1='{ry_}' x2='{mid}' y2='{ry_}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                        elems.append(f"<line x1='{mid}' y1='{ry_}' x2='{mid}' y2='{ty_}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")
                        elems.append(f"<line x1='{mid}' y1='{ty_}' x2='{tx_}' y2='{ty_}' stroke='{lc}' stroke-width='1.5' stroke-linecap='round'/>")

        sec_h  = PY + HDR + r1n * SH + PY
        cur_y += SEC_LBL_H + sec_h + SEC_GAP

    total_h = cur_y + PY
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{total_w}' height='{total_h}'>"
        f"<rect width='{total_w}' height='{total_h}' fill='#f8fafc'/>"
        + "".join(elems)
        + "</svg>"
    )


# ---------------------------------------------------------
# 5. INTERFAZ EN STREAMLIT
# ---------------------------------------------------------
st.set_page_config(page_title="H2 Tournament Manager", page_icon="🎮", layout="wide")
st.markdown("""
<style>
    .match-card-active  { border:2px solid #0284c7; border-radius:10px; padding:14px; margin-bottom:12px; background-color:rgba(2,132,199,0.05); }
    .match-card-winner  { border:2px solid #22c55e; border-radius:10px; padding:14px; margin-bottom:12px; background-color:rgba(34,197,94,0.08); }
    .match-card-loser   { border:1px dashed #94a3b8; border-radius:10px; padding:14px; margin-bottom:12px; opacity:0.5; filter:grayscale(80%); }
    .match-card-pending { border:1px dashed #475569; border-radius:10px; padding:14px; margin-bottom:12px; opacity:0.55; background-color:rgba(71,85,105,0.05); }
    .player-tag { display:inline-block; background-color:rgba(148,163,184,0.2); padding:2px 8px; border-radius:4px; font-size:0.85rem; margin:2px; font-weight:600; }
    .vs-divider { font-size:1.5rem; font-weight:900; text-align:center; color:#0284c7; margin-top:15px; }
    .block-container { padding-top:1rem !important; }
    .stMultiSelect [data-baseweb="tag"],[data-baseweb="tag"] { border-radius:6px !important; border:1px solid rgba(99,102,241,0.35) !important; background-color:rgba(99,102,241,0.12) !important; color:#4f46e5 !important; }
    .stMultiSelect [data-baseweb="tag"]:nth-child(5n+2) { background-color:rgba(16,185,129,0.12) !important; border-color:rgba(16,185,129,0.35) !important; color:#059669 !important; }
    .stMultiSelect [data-baseweb="tag"]:nth-child(5n+3) { background-color:rgba(245,158,11,0.12) !important; border-color:rgba(245,158,11,0.35) !important; color:#d97706 !important; }
    .stMultiSelect [data-baseweb="tag"]:nth-child(5n+4) { background-color:rgba(168,85,247,0.12) !important; border-color:rgba(168,85,247,0.35) !important; color:#9333ea !important; }
    .stMultiSelect [data-baseweb="tag"]:nth-child(5n+5) { background-color:rgba(20,184,166,0.12) !important; border-color:rgba(20,184,166,0.35) !important; color:#0d9488 !important; }
    [data-baseweb="input"]>div:focus-within,[data-baseweb="base-input"]:focus-within,[data-baseweb="select"]>div:focus-within,[data-baseweb="textarea"]>div:focus-within,.stTextInput>div>div:focus-within,.stSelectbox>div>div:focus-within,.stNumberInput>div>div:focus-within,.stTextArea>div>div:focus-within,.stMultiSelect>div>div:focus-within { box-shadow:none !important; border-color:#64748b !important; outline:none !important; }
    input:focus,textarea:focus,select:focus { outline:none !important; box-shadow:none !important; }
    .mode-badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:0.78rem; font-weight:700; margin-right:6px; }
</style>
""", unsafe_allow_html=True)

if "toast_msg" in st.session_state:
    st.toast(st.session_state["toast_msg"], icon="✅")
    del st.session_state["toast_msg"]

st.markdown("<style>.block-container{padding-top:3rem!important;} h1{font-size:2rem!important;margin:0!important;padding:0!important;}</style>", unsafe_allow_html=True)
st.title("H2 Tournament Manager")
st.divider()

if "_nav_redirect" in st.session_state:
    st.session_state["nav_menu"] = st.session_state.pop("_nav_redirect")

menu = st.sidebar.radio("Navegación", [
    "👥 Registro de Jugadores",
    "⚔️ Crear Torneo / Draft",
    "🔥 Arena en Vivo",
    "📊 Histórico & Llaves",
    "📋 Auditoría & Novedades",
    "👤 Perfil de Jugador",
    "⚙️ Panel de Admin (MMR & Rangos)"
], key="nav_menu")

RANK_POINTS = get_rank_config()

# ---------------------------------------------------------
# SECCIÓN 1: REGISTRO DE JUGADORES
# ---------------------------------------------------------
if menu == "👥 Registro de Jugadores":
    if "players_toast" in st.session_state:
        st.toast(st.session_state.pop("players_toast"), icon="⚠️")

    # ── REGISTER FORM (horizontal, full width) ───────────────────────────────
    with st.form("add_player_form", clear_on_submit=True):
        _fc1, _fc2, _fc3 = st.columns([3, 2, 1])
        name         = _fc1.text_input("Gamertag", placeholder="Nombre del jugador...")
        initial_rank = _fc2.selectbox("Rango inicial", list(RANK_POINTS.keys()), index=None, placeholder="Seleccionar rango...")
        _fc3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        submit = _fc3.form_submit_button("+ Registrar", use_container_width=True, type="primary")
        if submit:
            if not name:
                st.error("El Gamertag es obligatorio.")
            elif not initial_rank:
                st.error("Debes seleccionar un rango.")
            else:
                conn = sqlite3.connect('halo2_cartographer_pro.db')
                c = conn.cursor()
                existing = c.execute("SELECT id FROM players WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
                if existing:
                    st.error(f"'{name}' ya está registrado.")
                else:
                    pts_ini = RANK_PR_THRESHOLDS.get(initial_rank, 1000)
                    mmr_ini = compute_mmr_from_pr(pts_ini)
                    c.execute("""INSERT INTO players
                        (name, mmr, rank_category, tournament_points,
                         mmr_4v4, rank_4v4, pts_4v4,
                         mmr_2v2, rank_2v2, pts_2v2,
                         mmr_1v1, rank_1v1, pts_1v1)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (name, mmr_ini, initial_rank, pts_ini,
                         mmr_ini, initial_rank, pts_ini,
                         mmr_ini, initial_rank, pts_ini,
                         mmr_ini, initial_rank, pts_ini))
                    conn.commit()
                    log_action("Admin", "Crear Jugador", f"Registrado {name} con Rango {initial_rank} (PR: {pts_ini}, MMR: {mmr_ini})")
                    st.success(f"¡{name} registrado! Rango: **{initial_rank}** | PR: **{pts_ini}** | MMR: **{mmr_ini}**")
                conn.close()

    # ── PLAYERS TABLE ────────────────────────────────────────────────────────
    _rk_c_pl = {"D":"#475569","D+":"#57534e","C":"#059669","C+":"#047857","B":"#2563eb","B+":"#1d4ed8","A":"#7c3aed","A+":"#6d28d9","S":"#d97706","S+":"#b45309","S++":"#dc2626","S+++":"#b91c1c"}

    conn = sqlite3.connect('halo2_cartographer_pro.db')
    _players_raw = conn.execute("""
        SELECT p.id, p.name,
               p.rank_4v4, p.pts_4v4, p.mmr_4v4,
               p.rank_2v2, p.pts_2v2, p.mmr_2v2,
               p.rank_1v1, p.pts_1v1, p.mmr_1v1,
               COALESCE(w.wins,  0) AS wins,
               COALESCE(l.losses,0) AS losses
        FROM players p
        LEFT JOIN (
            SELECT ml.player_id, COUNT(DISTINCT m.id) AS wins
            FROM match_lineups ml JOIN matches m ON ml.match_id=m.id
            WHERE ml.team_id=m.winner_id AND m.status IN ('COMPLETED','Completed')
            GROUP BY ml.player_id
        ) w ON p.id=w.player_id
        LEFT JOIN (
            SELECT ml.player_id, COUNT(DISTINCT m.id) AS losses
            FROM match_lineups ml JOIN matches m ON ml.match_id=m.id
            WHERE ml.team_id!=m.winner_id AND m.winner_id IS NOT NULL
              AND (ml.team_id=m.team1_id OR ml.team_id=m.team2_id)
              AND m.status IN ('COMPLETED','Completed')
            GROUP BY ml.player_id
        ) l ON p.id=l.player_id
        ORDER BY p.pts_4v4 DESC
    """).fetchall()
    conn.close()

    _pl_sc1, _pl_sc2 = st.columns([3, 1])
    _pl_search = _pl_sc1.text_input("", placeholder="🔍  Buscar jugador...", label_visibility="collapsed", key="pl_search")
    _pl_rf = _pl_sc2.selectbox("", ["Todos"] + list(RANK_POINTS.keys()), index=0, key="pl_rank_filter", label_visibility="collapsed")

    _plist = _players_raw
    if _pl_search:
        _plist = [p for p in _plist if _pl_search.lower() in p[1].lower()]
    if _pl_rf != "Todos":
        _plist = [p for p in _plist if p[2] == _pl_rf]

    def _rk_b(rk):
        _c = _rk_c_pl.get(rk, "#475569")
        return f"<span style='background:{_c};color:#fff;padding:1px 7px;border-radius:4px;font-size:0.68rem;font-weight:800;'>{rk or '—'}</span>"

    _rows_pl = ""
    for _i, _p in enumerate(_plist):
        _pid, _nm, r4, pr4, m4, r2, pr2, m2, r1, pr1, m1, _wins, _losses = _p
        _bg = "#ffffff" if _i % 2 == 0 else "#f8fafc"
        _tot = _wins + _losses
        _wr = f"{round(_wins/_tot*100)}%" if _tot > 0 else "—"
        _rows_pl += (
            f"<tr style='background:{_bg};border-bottom:1px solid #f1f5f9;'>"
            f"<td style='padding:8px 12px;font-weight:700;color:#0f172a;font-size:0.84rem;white-space:nowrap;'>{_nm}</td>"
            f"<td style='padding:8px 6px;text-align:center;'>{_rk_b(r4)}</td>"
            f"<td style='padding:8px 6px;text-align:center;color:#334155;font-size:0.78rem;font-weight:600;'>{int(pr4)}</td>"
            f"<td style='padding:8px 12px 8px 4px;text-align:center;color:#64748b;font-size:0.75rem;border-right:1.5px solid #e2e8f0;'>{round(m4,1)}</td>"
            f"<td style='padding:8px 6px;text-align:center;'>{_rk_b(r2)}</td>"
            f"<td style='padding:8px 6px;text-align:center;color:#334155;font-size:0.78rem;font-weight:600;'>{int(pr2)}</td>"
            f"<td style='padding:8px 12px 8px 4px;text-align:center;color:#64748b;font-size:0.75rem;border-right:1.5px solid #e2e8f0;'>{round(m2,1)}</td>"
            f"<td style='padding:8px 6px;text-align:center;'>{_rk_b(r1)}</td>"
            f"<td style='padding:8px 6px;text-align:center;color:#334155;font-size:0.78rem;font-weight:600;'>{int(pr1)}</td>"
            f"<td style='padding:8px 12px 8px 4px;text-align:center;color:#64748b;font-size:0.75rem;border-right:1.5px solid #e2e8f0;'>{round(m1,1)}</td>"
            f"<td style='padding:8px 10px;text-align:center;color:#16a34a;font-weight:800;font-size:0.84rem;'>{_wins}</td>"
            f"<td style='padding:8px 10px;text-align:center;color:#dc2626;font-weight:800;font-size:0.84rem;'>{_losses}</td>"
            f"<td style='padding:8px 10px;text-align:center;color:#64748b;font-size:0.78rem;font-weight:600;'>{_wr}</td>"
            f"</tr>"
        )

    _thc  = "padding:6px 6px;text-align:center;font-size:0.62rem;font-weight:800;color:#94a3b8;letter-spacing:0.1em;text-transform:uppercase;"
    _thl  = "padding:6px 12px;text-align:left;font-size:0.62rem;font-weight:800;color:#94a3b8;letter-spacing:0.1em;text-transform:uppercase;"
    _thg  = "padding:5px 8px;text-align:center;font-size:0.62rem;font-weight:800;letter-spacing:0.1em;text-transform:uppercase;border-bottom:2px solid;"
    _table_pl = (
        f"<div style='overflow-x:auto;border:1.5px solid #e2e8f0;border-radius:12px;background:#fff;margin-top:8px;'>"
        f"<div style='font-size:0.7rem;color:#94a3b8;padding:7px 12px 5px;border-bottom:1px solid #f1f5f9;'>{len(_plist)} jugador(es)</div>"
        f"<table style='width:100%;border-collapse:collapse;min-width:700px;'>"
        f"<thead>"
        f"<tr style='background:#f8fafc;'>"
        f"<th style='{_thl}' rowspan='2'>Jugador</th>"
        f"<th colspan='3' style='{_thg}color:#0284c7;border-color:#0284c7;'>4 v 4</th>"
        f"<th colspan='3' style='{_thg}color:#7c3aed;border-color:#7c3aed;'>2 v 2</th>"
        f"<th colspan='3' style='{_thg}color:#059669;border-color:#059669;'>1 v 1</th>"
        f"<th style='{_thc}color:#16a34a;' rowspan='2'>V</th>"
        f"<th style='{_thc}color:#dc2626;' rowspan='2'>D</th>"
        f"<th style='{_thc}' rowspan='2'>W%</th>"
        f"</tr>"
        f"<tr style='background:#f8fafc;border-bottom:2px solid #e2e8f0;'>"
        f"<th style='{_thc}'>Rango</th><th style='{_thc}'>PR</th><th style='{_thc}border-right:1.5px solid #e2e8f0;'>MMR</th>"
        f"<th style='{_thc}'>Rango</th><th style='{_thc}'>PR</th><th style='{_thc}border-right:1.5px solid #e2e8f0;'>MMR</th>"
        f"<th style='{_thc}'>Rango</th><th style='{_thc}'>PR</th><th style='{_thc}border-right:1.5px solid #e2e8f0;'>MMR</th>"
        f"</tr>"
        f"</thead>"
        f"<tbody>{_rows_pl}</tbody>"
        f"</table></div>"
    )
    st.markdown(_table_pl, unsafe_allow_html=True)

    # ── EDIT SECTION ────────────────────────────────────────────────────────
    with st.expander("✏️ Editar jugadores", expanded=False):
        if "players_force_mode" in st.session_state:
            st.session_state["players_mode_view"] = st.session_state.pop("players_force_mode")

        _pending_mode = None
        for _m in ["4v4", "2v2", "1v1"]:
            _v = st.session_state.get(f"_tbl_v_{_m}", 0)
            _es = st.session_state.get(f"players_table_{_m}_v{_v}", {})
            if _es.get("edited_rows") or _es.get("added_rows"):
                _pending_mode = _m
                break

        mode_view = st.radio("Modalidad:", ["4v4", "2v2", "1v1"], horizontal=True, key="players_mode_view")

        if _pending_mode and mode_view != _pending_mode:
            st.session_state["players_force_mode"] = _pending_mode
            st.session_state["players_toast"] = f"Guarda o cancela los cambios en {_pending_mode} antes de cambiar de modalidad."
            st.rerun()

        mc_v, rc_v, pc_v = MODE_COLS[mode_view]
        _tbl_ver  = st.session_state.get(f"_tbl_v_{mode_view}", 0)
        table_key = f"players_table_{mode_view}_v{_tbl_ver}"

        if st.session_state.get("_clear_filters"):
            st.session_state["_clear_filters"] = False
            st.session_state["search_gamertag"] = ""
            st.session_state["rank_filter"] = "Todos"

        fcol1, fcol2, fcol3 = st.columns([2, 1, 0.5])
        search      = fcol1.text_input("🔍 Buscar", placeholder="Gamertag...", key="search_gamertag")
        rank_filter = fcol2.selectbox("Filtrar Rango", ["Todos"] + list(RANK_POINTS.keys()), index=0, key="rank_filter")
        fcol3.markdown("<div style='margin-top:26px'>", unsafe_allow_html=True)
        if fcol3.button("✕", help="Limpiar filtros", use_container_width=True):
            st.session_state["_clear_filters"] = True
            st.rerun()
        fcol3.markdown("</div>", unsafe_allow_html=True)

        conn = sqlite3.connect('halo2_cartographer_pro.db')
        players_df = pd.read_sql(f"""
            SELECT id, name AS Gamertag,
                   {rc_v} AS Rango,
                   {mc_v} AS 'MMR',
                   {pc_v} AS 'PR (Puntos de Rango)'
            FROM players ORDER BY {pc_v} DESC
        """, conn)
        conn.close()

        table_df = players_df.copy()
        if search:
            table_df = table_df[table_df["Gamertag"].str.contains(search, case=False, na=False, regex=False)]
        if rank_filter != "Todos":
            table_df = table_df[table_df["Rango"] == rank_filter]
        table_df = table_df.copy()

        edited_df = st.data_editor(
            table_df,
            column_config={
                "id": None,
                "Gamertag": st.column_config.TextColumn("Gamertag", required=True),
                "Rango": st.column_config.SelectboxColumn(f"Rango ({mode_view})", options=list(RANK_POINTS.keys()), required=True),
                "MMR": st.column_config.NumberColumn(f"MMR ({mode_view})", disabled=True, format="%.2f"),
                "PR (Puntos de Rango)": st.column_config.NumberColumn(f"PR ({mode_view})", disabled=True, format="%.0f"),
            },
            hide_index=True, use_container_width=True, key=table_key
        )

        orig_map = {int(r["id"]): r for _, r in table_df.iterrows()}
        changed = [
            row["Gamertag"] for _, row in edited_df.iterrows()
            if int(row["id"]) in orig_map and (
                orig_map[int(row["id"])]["Gamertag"] != row["Gamertag"] or
                orig_map[int(row["id"])]["Rango"] != row["Rango"]
            )
        ]

        if changed:
            badges = " ".join([
                f"<span style='background:#fef08a;color:#713f12;padding:2px 10px;border-radius:6px;font-size:0.82rem;font-weight:600'>{g}</span>"
                for g in changed
            ])
            st.markdown(f"⚠️ **Cambios sin guardar ({mode_view}):** {badges}", unsafe_allow_html=True)
            btn_save_col, btn_cancel_col = st.columns(2)
            do_save   = btn_save_col.button("💾 Guardar cambios", use_container_width=True, type="primary", key="save_changes_btn")
            do_cancel = btn_cancel_col.button("✕ Cancelar cambios", use_container_width=True, key="cancel_changes_btn")
            if do_cancel:
                st.session_state[f"_tbl_v_{mode_view}"] = _tbl_ver + 1
                st.rerun()
        else:
            do_save = st.button("💾 Guardar cambios", use_container_width=True, disabled=True)

        if do_save:
            conn = sqlite3.connect('halo2_cartographer_pro.db')
            c = conn.cursor()
            errors = []
            for _, row in edited_df.iterrows():
                try:
                    new_pts = RANK_PR_THRESHOLDS.get(row["Rango"], int(row["PR (Puntos de Rango)"]))
                    new_mmr = compute_mmr_from_pr(new_pts)
                    c.execute(f"UPDATE players SET name=?, {rc_v}=?, {pc_v}=?, {mc_v}=? WHERE id=?",
                              (row["Gamertag"], row["Rango"], new_pts, new_mmr, int(row["id"])))
                except Exception:
                    errors.append(row["Gamertag"])
            conn.commit(); conn.close()
            if errors: st.error(f"No se pudo guardar: {', '.join(errors)}")
            else:
                log_action("Admin", "Editar Jugadores", f"Cambios guardados ({mode_view})")
                st.session_state["toast_msg"] = f"¡Tabla de jugadores ({mode_view}) actualizada!"
                st.session_state[f"_tbl_v_{mode_view}"] = _tbl_ver + 1
                st.rerun()

# ---------------------------------------------------------
# SECCIÓN 2: CREAR TORNEO / DRAFT
# ---------------------------------------------------------
elif menu == "⚔️ Crear Torneo / Draft":
    if st.session_state.get("_clear_torneo"):
        st.session_state["_clear_torneo"] = False
        for k in ["torneo_admin", "torneo_name", "torneo_players", "_draft_view_active", "_draft_admin", "_draft_name", "_draft_players"]:
            st.session_state.pop(k, None)

    _draft_view = st.session_state.get("_draft_view_active", False)

    # ── Helper launch (self-contained: reads everything from session_state) ──
    def _launch_tournament(teams_data_dict):
        _tn  = st.session_state.get("_draft_name", "") or st.session_state.get("torneo_name", "")
        _an  = st.session_state.get("_draft_admin", "") or st.session_state.get("torneo_admin", "")
        _cat = st.session_state.get("torneo_category", "4v4")
        _fmt = st.session_state.get("torneo_fmt", "Eliminación Directa")
        _bde = st.session_state.get("torneo_bde", True)
        _mc, _rc, _pc = MODE_COLS[_cat]
        _c0  = sqlite3.connect('halo2_cartographer_pro.db')
        _pd2 = {p[1]: p for p in _c0.execute(f"SELECT id, name, {_mc}, {_rc}, {_pc} FROM players").fetchall()}
        _c0.close()
        conn2 = sqlite3.connect('halo2_cartographer_pro.db')
        c2_   = conn2.cursor()
        date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        c2_.execute(
            "INSERT INTO tournaments (name,created_by,category,format,date,status,bracket_reset_enabled) VALUES (?,?,?,?,?,?,?)",
            (_tn, _an, _cat, _fmt, date_str, "In Progress", 1 if _bde else 0)
        )
        t_id = c2_.lastrowid
        team_ids = []
        for t_name, plist in teams_data_dict.items():
            t_points = sum(_pd2[p][2] for p in plist if p in _pd2)
            c2_.execute("INSERT INTO teams (tournament_id,team_name,total_points) VALUES (?,?,?)", (t_id, t_name, t_points))
            team_ids.append((c2_.lastrowid, plist))
        seed_ids = [tid for tid, _ in team_ids]
        tpm = {tid: plist for tid, plist in team_ids}
        if _fmt == "Doble Eliminación": create_bracket_double(t_id, seed_ids, _bde, conn2)
        else:                           create_bracket_single(t_id, seed_ids, conn2)
        r1 = c2_.execute(
            "SELECT id,team1_id,team2_id FROM matches WHERE tournament_id=? AND round_number=1 AND bracket_type='UPPER'",
            (t_id,)
        ).fetchall()
        for m_id, t1, t2 in r1:
            for team_id in [t1, t2]:
                if team_id and team_id in tpm:
                    for p_name in tpm[team_id]:
                        if p_name in _pd2:
                            c2_.execute(
                                "INSERT INTO match_lineups (match_id,team_id,player_id) VALUES (?,?,?)",
                                (m_id, team_id, _pd2[p_name][0])
                            )
        conn2.commit(); conn2.close()
        st.session_state["toast_msg"]           = f"¡Torneo '{_tn}' iniciado!"
        st.session_state["_clear_torneo"]       = True
        st.session_state["_draft_view_active"]  = False
        st.session_state["_nav_redirect"]       = "🔥 Arena en Vivo"
        st.rerun()

    # ══════════════════════════════════════════════════════════════════════
    # VISTA 1: Formulario de configuración
    # ══════════════════════════════════════════════════════════════════════
    if not _draft_view:
        st.header("⚔️ Crear Torneo / Recocha")

        c1, c2 = st.columns(2)
        admin_name      = c1.text_input("Organizador", key="torneo_admin", placeholder="Nombre del organizador")
        tournament_name = c2.text_input("Nombre del Torneo", key="torneo_name", placeholder=f"Ej: Torneo - {datetime.now().strftime('%Y-%m-%d')}")

        c3, c4, c5 = st.columns(3)
        category   = c3.selectbox("Modalidad",           ["4v4", "2v2", "1v1"],                           key="torneo_category")
        fmt        = c4.selectbox("Formato",              ["Eliminación Directa", "Doble Eliminación"],    key="torneo_fmt")
        draft_mode = c5.selectbox("Método de Selección", ["Snake Draft (Balanceado Auto)", "Draft por Bolsas"], key="torneo_draft_mode")

        players_per_team = 4 if category == "4v4" else (2 if category == "2v2" else 1)
        mc_t, rc_t, pc_t = MODE_COLS[category]

        conn = sqlite3.connect('halo2_cartographer_pro.db')
        players_raw = conn.execute(f"SELECT id, name, {mc_t}, {rc_t}, {pc_t} FROM players ORDER BY {mc_t} DESC, {pc_t} DESC").fetchall()
        conn.close()
        player_dict = {p[1]: p for p in players_raw}

        selected_players = st.multiselect("Seleccionar Jugadores Inscritos:", list(player_dict.keys()), key="torneo_players")

        total_selected = len(selected_players)
        calc_teams = total_selected // players_per_team if players_per_team > 0 else 0
        leftover   = total_selected % players_per_team if players_per_team > 0 else 0

        st.info(f"💡 **Inscritos:** {total_selected} | **Equipos completos:** {calc_teams} ({players_per_team} jugadores por equipo) | **Modalidad:** {category}")
        if leftover > 0:
            faltan = players_per_team - leftover
            st.warning(f"⚠️ {leftover} jugador(es) no completan equipo — faltan **{faltan}** más, o deselecciona ese(s) jugador(es).")

        bracket_reset_enabled = True
        if fmt == "Doble Eliminación":
            bracket_reset_enabled = st.checkbox(
                "🔄 Bracket Reset habilitado", value=True,
                help="Si el campeón del Lower Bracket gana la Gran Final, se juega un partido decisivo adicional.",
                key="torneo_bde"
            )

        # ── Snake Draft ───────────────────────────────────────────────────
        if draft_mode == "Snake Draft (Balanceado Auto)":
            if st.button("🚀 Confirmar e Iniciar Torneo", type="primary", use_container_width=True):
                if not admin_name.strip():        st.error("El campo Organizador es obligatorio.")
                elif not tournament_name.strip(): st.error("El campo Nombre del Torneo es obligatorio.")
                elif not selected_players:        st.error("Debes seleccionar al menos los jugadores necesarios.")
                elif calc_teams < 2:              st.error("Se requieren al menos 2 equipos completos para iniciar el torneo.")
                elif fmt == "Doble Eliminación" and calc_teams < 3:
                    st.error("Doble Eliminación requiere al menos 3 equipos.")
                else:
                    sorted_sel = sorted(
                        selected_players[:calc_teams * players_per_team],
                        key=lambda x: (player_dict[x][2], player_dict[x][4]), reverse=True
                    )
                    teams_data = {f"Equipo {sorted_sel[i]}": [] for i in range(calc_teams)}
                    t_names    = list(teams_data.keys())
                    for i, p_name in enumerate(sorted_sel):
                        ronda = i // calc_teams
                        pos   = i % calc_teams
                        if ronda % 2 == 1: pos = calc_teams - 1 - pos
                        teams_data[t_names[pos]].append(p_name)
                    _launch_tournament(teams_data)

        # ── Draft por Bolsas: botón para ir a la vista de equipos ─────────
        elif draft_mode == "Draft por Bolsas":
            if calc_teams < 2 or total_selected < calc_teams * players_per_team:
                st.info("Selecciona suficientes jugadores para al menos 2 equipos completos.")
            else:
                if st.button("⚔️ Armar Equipos", type="primary", use_container_width=True, key="go_draft"):
                    if not admin_name.strip():
                        st.error("El campo Organizador es obligatorio.")
                    elif not tournament_name.strip():
                        st.error("El campo Nombre del Torneo es obligatorio.")
                    else:
                        st.session_state["_draft_players"] = selected_players
                        st.session_state["_draft_admin"]   = admin_name
                        st.session_state["_draft_name"]    = tournament_name
                        st.session_state["_draft_view_active"] = True
                        st.rerun()

    # ══════════════════════════════════════════════════════════════════════
    # VISTA 2: Draft por Bolsas (DnD)
    # ══════════════════════════════════════════════════════════════════════
    else:
        category         = st.session_state.get("torneo_category", "4v4")
        fmt              = st.session_state.get("torneo_fmt", "Eliminación Directa")
        selected_players = st.session_state.get("_draft_players", [])
        players_per_team = 4 if category == "4v4" else (2 if category == "2v2" else 1)
        mc_t, rc_t, pc_t = MODE_COLS[category]

        conn = sqlite3.connect('halo2_cartographer_pro.db')
        players_raw = conn.execute(f"SELECT id, name, {mc_t}, {rc_t}, {pc_t} FROM players ORDER BY {mc_t} DESC, {pc_t} DESC").fetchall()
        conn.close()
        player_dict = {p[1]: p for p in players_raw}

        total_selected = len(selected_players)
        calc_teams     = total_selected // players_per_team if players_per_team > 0 else 0

        if st.button("← Volver al Formulario", key="back_to_form"):
            st.session_state["_draft_view_active"] = False
            st.rerun()

        _RC = {
            "D": "#94a3b8", "D+": "#a8a29e", "C": "#10b981", "C+": "#059669",
            "B": "#3b82f6", "B+": "#2563eb", "A": "#8b5cf6", "A+": "#7c3aed",
            "S": "#f59e0b", "S+": "#d97706", "S++": "#ef4444", "S+++": "#dc2626"
        }
        _TC = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4", "#f43f5e", "#84cc16"]

        if calc_teams < 2 or total_selected < calc_teams * players_per_team:
            st.warning("No hay suficientes jugadores seleccionados. Vuelve al formulario.")
        else:
            sorted_sel = sorted(
                selected_players[:calc_teams * players_per_team],
                key=lambda x: (player_dict[x][2], player_dict[x][4]), reverse=True
            )
            _tiers = [sorted_sel[_ti * calc_teams:(_ti + 1) * calc_teams] for _ti in range(players_per_team)]

            _dbh = hash(tuple(sorted_sel))
            if st.session_state.get("_dbh") != _dbh:
                st.session_state["_dbh"]    = _dbh
                st.session_state["_dteams"] = [[p] for p in _tiers[0]]
                st.session_state["_dbols"]  = [list(_tiers[_pi]) for _pi in range(1, players_per_team)]

            def _get_cap(team_players):
                valid = [p for p in team_players if p in player_dict]
                if not valid:
                    return None
                return max(valid, key=lambda x: (player_dict[x][2], player_dict[x][4]))

            _pools_ss = st.session_state.get("_dbols",  [list(_t) for _t in _tiers])
            _teams_ss = st.session_state.get("_dteams", [[] for _ in range(calc_teams)])

            _pdata = {
                name: {"rank": row[3] or "?", "mmr": float(row[2] or 0), "pr": int(row[4] or 0)}
                for name, row in player_dict.items()
            }
            _dnd_result = _DND_COMPONENT(
                pools=_pools_ss,
                teams=_teams_ss,
                player_data=_pdata,
                config={"players_per_team": players_per_team, "balance_tolerance": BALANCE_TOLERANCE},
                key="dnd_bolsas",
                default=None,
            )
            if _dnd_result is not None:
                _action = _dnd_result.get("action")
                if _action == "autofill":
                    _bp = [list(b) for b in st.session_state.get("_dbols", [])]
                    _bt = [list(t) for t in st.session_state.get("_dteams", [[] for _ in range(calc_teams)])]
                    for _pool in _bp:
                        random.shuffle(_pool)
                        for _pp in list(_pool):
                            _needs = [_i for _i, _t in enumerate(_bt) if len(_t) < players_per_team]
                            if not _needs:
                                break
                            _tgt = min(_needs, key=lambda _i: len(_bt[_i]))
                            _bt[_tgt].append(_pp)
                            _pool.remove(_pp)
                    st.session_state["_dbols"]  = _bp
                    st.session_state["_dteams"] = _bt
                    st.rerun()
                elif _action == "clear":
                    st.session_state["_dteams"] = [[p] for p in _tiers[0]]
                    st.session_state["_dbols"]  = [list(_tiers[_pi]) for _pi in range(1, players_per_team)]
                    st.rerun()
                elif _action == "launch":
                    _tss_l = st.session_state.get("_dteams", [[] for _ in range(calc_teams)])
                    _final_teams = {}
                    for _tii2, _tp3 in enumerate(_tss_l):
                        _cap3 = _get_cap(_tp3)
                        _final_teams[f"Equipo {_cap3 or (_tii2 + 1)}"] = _tp3
                    _incp = [t for t, p in _final_teams.items() if len(p) < players_per_team]
                    if not st.session_state.get("_draft_admin", "").strip():
                        st.error("El campo Organizador es obligatorio.")
                    elif not st.session_state.get("_draft_name", "").strip():
                        st.error("El campo Nombre del Torneo es obligatorio.")
                    elif _incp:
                        st.error(f"Faltan jugadores en: {', '.join(_incp)}")
                    elif fmt == "Doble Eliminación" and len(_final_teams) < 3:
                        st.error("Doble Eliminación requiere al menos 3 equipos.")
                    else:
                        _launch_tournament(_final_teams)
                else:
                    _np = _dnd_result.get("pools", _pools_ss)
                    _nt = _dnd_result.get("teams", _teams_ss)
                    if _np != _pools_ss or _nt != _teams_ss:
                        st.session_state["_dbols"]  = _np
                        st.session_state["_dteams"] = _nt
                        st.rerun()



# ---------------------------------------------------------
# SECCIÓN 3: ARENA EN VIVO
# ---------------------------------------------------------
elif menu == "🔥 Arena en Vivo":
    st.markdown(
        "<div style='font-size:1.35rem;font-weight:800;color:#0f172a;margin-bottom:4px;'>🔥 Arena en Vivo</div>"
        "<div style='font-size:0.78rem;color:#64748b;margin-bottom:20px;letter-spacing:0.03em;'>"
        "Bracket de enfrentamientos en tiempo real</div>",
        unsafe_allow_html=True
    )
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    tournaments = conn.execute("SELECT id,name FROM tournaments WHERE status='In Progress'").fetchall()

    if not tournaments:
        st.info("No hay torneos en curso actualmente.")
        conn.close()
    else:
        t_options  = {f"#{t[0]} - {t[1]}": t[0] for t in tournaments}
        selected_t = st.selectbox("Torneo Activo", list(t_options.keys()))
        t_id       = t_options[selected_t]

        t_meta  = conn.execute("SELECT format, bracket_reset_enabled, category, created_by FROM tournaments WHERE id=?", (t_id,)).fetchone()
        t_fmt     = t_meta[0] if t_meta else "Eliminación Directa"
        t_cat     = t_meta[2] if t_meta else "4v4"
        t_creator = t_meta[3] if t_meta else "Admin"
        mc_a, rc_a, _ = MODE_COLS.get(t_cat, MODE_COLS["4v4"])

        is_old_fmt = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE tournament_id=? AND round_name='Primera Ronda'", (t_id,)).fetchone()[0] > 0

        if is_old_fmt:
            matches = conn.execute("""
                SELECT m.id,m.round_name,t1.team_name,t2.team_name,m.status,m.team1_id,m.team2_id,m.winner_id
                FROM matches m JOIN teams t1 ON m.team1_id=t1.id JOIN teams t2 ON m.team2_id=t2.id
                WHERE m.tournament_id=? ORDER BY m.id ASC
            """, (t_id,)).fetchall()
            rondas = {}
            for m in matches: rondas.setdefault(m[1], []).append(m)
            for round_title, round_matches in rondas.items():
                st.subheader(f"🏆 {round_title}")
                for m in round_matches:
                    match_id,_,t1_name,t2_name,status,t1_id,t2_id,winner_id = m
                    players_t1 = conn.execute(f"SELECT DISTINCT p.name,p.{rc_a},p.{mc_a} FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (match_id,t1_id)).fetchall()
                    players_t2 = conn.execute(f"SELECT DISTINCT p.name,p.{rc_a},p.{mc_a} FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (match_id,t2_id)).fetchall()
                    t1_tags = "".join([f"<span class='player-tag'>{p[0]} ({p[1]} - {int(p[2])} MMR)</span>" for p in players_t1])
                    t2_tags = "".join([f"<span class='player-tag'>{p[0]} ({p[1]} - {int(p[2])} MMR)</span>" for p in players_t2])
                    if status == "Pending": c1s, c2s = "match-card-active", "match-card-active"
                    else: c1s = "match-card-winner" if winner_id==t1_id else "match-card-loser"; c2s = "match-card-winner" if winner_id==t2_id else "match-card-loser"
                    col_l,col_v,col_r = st.columns([5,1,5])
                    with col_l: st.markdown(f"<div class='{c1s}'><h3>{t1_name} {'🏆 (GANADOR)' if winner_id==t1_id else ''}</h3><p><b>MMR Promedio ({t_cat}):</b> {round(sum(p[2] for p in players_t1)/len(players_t1),1) if players_t1 else 0}</p><div>{t1_tags}</div></div>", unsafe_allow_html=True)
                    with col_v: st.markdown("<div class='vs-divider'>VS</div>", unsafe_allow_html=True)
                    with col_r: st.markdown(f"<div class='{c2s}'><h3>{t2_name} {'🏆 (GANADOR)' if winner_id==t2_id else ''}</h3><p><b>MMR Promedio ({t_cat}):</b> {round(sum(p[2] for p in players_t2)/len(players_t2),1) if players_t2 else 0}</p><div>{t2_tags}</div></div>", unsafe_allow_html=True)
                    if status == "Pending":
                        w1,w2 = st.columns(2)
                        if w1.button(f"Gana {t1_name}", key=f"btn_w1_{match_id}", use_container_width=True):
                            conn.close(); delta = process_match_victory(t1_id,t2_id,match_id)
                            st.session_state["toast_msg"] = f"¡Victoria de {t1_name}! (+{delta} MMR)"; st.rerun()
                        if w2.button(f"Gana {t2_name}", key=f"btn_w2_{match_id}", use_container_width=True):
                            conn.close(); delta = process_match_victory(t2_id,t1_id,match_id)
                            st.session_state["toast_msg"] = f"¡Victoria de {t2_name}! (+{delta} MMR)"; st.rerun()
                    st.divider()
            conn.close()
        else:
            judge_name_arena = t_creator
            st.info(f"👤 Organizador / Juez: **{t_creator}**")
            has_lb = conn.execute("SELECT COUNT(*) FROM matches WHERE tournament_id=? AND bracket_type='LOWER'", (t_id,)).fetchone()[0] > 0
            if has_lb:
                tab_ub,tab_lb,tab_gf,tab_all = st.tabs(["⬆️ Upper Bracket","⬇️ Lower Bracket","🏆 Gran Final","📊 Completo"])
                bracket_sections = [("UPPER",tab_ub),("LOWER",tab_lb),("GRAND_FINAL",tab_gf)]
            else:
                tab_ub,tab_all = st.tabs(["🏆 Bracket","📊 Completo"])
                bracket_sections = [("UPPER",tab_ub)]

            for btype, tab_ctx in bracket_sections:
                with tab_ctx:
                    bmatches = conn.execute("""
                        SELECT m.id,m.round_name,t1.team_name,t2.team_name,
                               m.status,m.team1_id,m.team2_id,m.winner_id,m.is_bracket_reset,
                               m.round_number,m.position
                        FROM matches m LEFT JOIN teams t1 ON m.team1_id=t1.id LEFT JOIN teams t2 ON m.team2_id=t2.id
                        WHERE m.tournament_id=? AND m.bracket_type=? ORDER BY m.round_number ASC,m.position ASC
                    """, (t_id,btype)).fetchall()

                    # ── BRACKET VISUALIZATION ──────────────────────────────
                    st.markdown(_render_bracket_svg(bmatches, accent="#0284c7"), unsafe_allow_html=True)

                    # ── MATCH CONTROLS ─────────────────────────────────────
                    _active_bm = [bm for bm in bmatches if bm[4] in ('READY','IN_PROGRESS')]
                    if not _active_bm:
                        if all(bm[4] in ('COMPLETED','BYE_COMPLETED','PENDING') for bm in bmatches):
                            if any(bm[4] in ('COMPLETED','BYE_COMPLETED') for bm in bmatches):
                                st.success("✅ Todas las partidas de este bracket han sido completadas.")
                            else:
                                st.info("⏳ Las partidas están pendientes de rondas anteriores.")
                    for bm in bmatches:
                        m_id,rn,t1n,t2n,st_val,t1i,t2i,win_id,is_reset,_rnum,_rpos = bm
                        if st_val not in ('READY','IN_PROGRESS'):
                            continue
                        is_ready = st_val == 'READY'
                        is_ip    = st_val == 'IN_PROGRESS'
                        t1_disp  = t1n or "Por definir"; t2_disp = t2n or "Por definir"
                        pl_t1 = conn.execute(f"SELECT DISTINCT p.name,p.{rc_a},p.{mc_a} FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (m_id,t1i)).fetchall() if t1i else []
                        pl_t2 = conn.execute(f"SELECT DISTINCT p.name,p.{rc_a},p.{mc_a} FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (m_id,t2i)).fetchall() if t2i else []
                        avg1  = round(sum(p[2] for p in pl_t1),1) if pl_t1 else "—"
                        avg2  = round(sum(p[2] for p in pl_t2),1) if pl_t2 else "—"
                        tc    = "#f97316" if is_ip else "#0284c7"
                        badge = "🔴 EN PROGRESO" if is_ip else "⏳ LISTA"
                        bbc   = "rgba(249,115,22,0.1)" if is_ip else "rgba(2,132,199,0.1)"
                        bfc   = "#c2410c" if is_ip else "#0369a1"
                        _rk_c = {"D":"#475569","D+":"#57534e","C":"#059669","C+":"#047857","B":"#2563eb","B+":"#1d4ed8","A":"#7c3aed","A+":"#6d28d9","S":"#d97706","S+":"#b45309","S++":"#dc2626","S+++":"#b91c1c"}
                        t1_pt = "".join([f"<span style='display:inline-flex;align-items:center;background:#f1f5f9;border-radius:4px;font-size:0.7rem;font-weight:600;margin:2px 3px 2px 0;overflow:hidden;'><span style='padding:1px 6px;color:#334155;'>{p[0]}</span><span style='background:{_rk_c.get(p[1],'#475569')};color:#fff;padding:1px 6px;font-weight:800;'>{p[1]}</span></span>" for p in pl_t1])
                        t2_pt = "".join([f"<span style='display:inline-flex;align-items:center;background:#f1f5f9;border-radius:4px;font-size:0.7rem;font-weight:600;margin:2px 0 2px 3px;overflow:hidden;'><span style='padding:1px 6px;color:#334155;'>{p[0]}</span><span style='background:{_rk_c.get(p[1],'#475569')};color:#fff;padding:1px 6px;font-weight:800;'>{p[1]}</span></span>" for p in pl_t2])
                        cid   = f"bmc{m_id}"
                        st.markdown(f"""<style>
div[data-testid="stVerticalBlockBorderWrapper"]:has(#{cid}){{
    border:1.5px solid #e2e8f0 !important;
    border-top:3px solid {tc} !important;
    background:#ffffff !important;
    box-shadow:0 2px 8px rgba(0,0,0,0.07) !important;
    border-radius:10px !important;
    margin-bottom:16px !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(#{cid})>div{{
    padding:0 10px 8px !important;
    margin:0 !important;
}}
div[data-testid="stMarkdown"]:has(span#{cid}){{
    display:none !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(#{cid}) input[type="number"]{{
    height:26px !important;min-height:26px !important;
    padding:0 4px !important;font-size:0.78rem !important;
}}</style>""", unsafe_allow_html=True)
                        score_a = score_b = 0
                        with st.container(border=True):
                            st.markdown(f'<span id="{cid}" style="display:none;"></span>', unsafe_allow_html=True)
                            st.markdown(f"<div style='display:flex;align-items:center;justify-content:space-between;margin:0 0 6px 0;padding:0;'><span style='font-size:1.05rem;font-weight:800;letter-spacing:0.04em;text-transform:uppercase;color:{tc};line-height:1.2;'>{'🔄 ' if is_reset else ''}{rn}</span><span style='background:{bbc};color:{bfc};padding:2px 8px;border-radius:10px;font-size:0.65rem;font-weight:700;'>{badge}</span></div><hr style='margin:0 0 6px;border:none;border-top:1px solid #f1f5f9;'>", unsafe_allow_html=True)
                            tc1, tvc, tc2 = st.columns([4, 3, 4] if is_ip else [5, 2, 5])
                            tc1.markdown(f"<div><div style='font-size:1.05rem;font-weight:800;color:{tc};'>{t1_disp}</div><div style='font-size:0.67rem;color:#64748b;margin:2px 0 4px;'>MMR ({t_cat}): {avg1}</div><div>{t1_pt}</div></div>", unsafe_allow_html=True)
                            tc2.markdown(f"<div style='text-align:right;'><div style='font-size:1.05rem;font-weight:800;color:{tc};'>{t2_disp}</div><div style='font-size:0.67rem;color:#64748b;margin:2px 0 4px;'>MMR ({t_cat}): {avg2}</div><div style='text-align:right;'>{t2_pt}</div></div>", unsafe_allow_html=True)
                            with tvc:
                                st.markdown("<div style='text-align:center;padding-top:6px;font-size:0.85rem;font-weight:900;color:#cbd5e1;'>VS</div>", unsafe_allow_html=True)
                                if is_ready and t1i and t2i:
                                    if st.button("▶️ Empezar", key=f"btn_start_{m_id}", use_container_width=True):
                                        conn.close()
                                        start_match(m_id, judge_name_arena or "Admin", t_cat)
                                        st.session_state["toast_msg"] = f"¡Iniciada: {t1_disp} vs {t2_disp}!"
                                        st.rerun()
                                elif is_ip and t1i and t2i:
                                    _sa_col, _sb_col = st.columns(2)
                                    score_a = _sa_col.number_input("", min_value=0, max_value=99, value=0, step=1, key=f"sa_{m_id}", label_visibility="collapsed")
                                    score_b = _sb_col.number_input("", min_value=0, max_value=99, value=0, step=1, key=f"sb_{m_id}", label_visibility="collapsed")
                                    is_tie = int(score_a) == int(score_b)
                                    if is_tie:
                                        st.markdown("<div style='text-align:center;color:#ef4444;font-size:0.68rem;font-weight:700;padding:3px 0 0;'>⚠️ Empate</div>", unsafe_allow_html=True)
                                    else:
                                        score_str       = f"{int(score_a)} - {int(score_b)}"
                                        inferred_winner = t1i if score_a > score_b else t2i
                                        winner_label    = t1_disp if score_a > score_b else t2_disp
                                        inferred_loser  = t2i  if score_a > score_b else t1i
                                        st.markdown(f"<div style='text-align:center;font-size:0.72rem;color:#64748b;font-weight:700;margin:2px 0;'>{score_str}</div>", unsafe_allow_html=True)
                                        if st.button(f"✅ Gana {winner_label}", key=f"btn_fin_{m_id}", use_container_width=True, type="primary"):
                                            conn.close()
                                            delta = process_match_result(m_id, inferred_winner, inferred_loser,
                                                registered_by=judge_name_arena or "Admin", score=score_str)
                                            st.session_state["toast_msg"] = f"¡Finalizado! Gana {winner_label} (+{delta} PR)"
                                            st.rerun()
                            if is_ip and t1i and t2i:
                                with st.expander("📝 Sustitución", expanded=False):
                                    all_players_arena = conn.execute("SELECT id, name FROM players ORDER BY name").fetchall()
                                    all_match_player_ids = {r[0] for r in conn.execute(
                                        "SELECT DISTINCT player_id FROM match_lineups WHERE match_id=?", (m_id,)
                                    ).fetchall()}
                                    _sc1,_sc2,_sc3,_sc4,_sc5 = st.columns([2,2,2,3,1])
                                    sub_team_sel    = _sc1.selectbox("Equipo", [t1_disp, t2_disp], key=f"sub_team_{m_id}", label_visibility="collapsed")
                                    sub_team_id     = t1i if sub_team_sel == t1_disp else t2i
                                    cur_lineup_names = [p[0] for p in pl_t1] if sub_team_sel == t1_disp else [p[0] for p in pl_t2]
                                    player_out_name = _sc2.selectbox("Sale", cur_lineup_names, key=f"sub_out_{m_id}", label_visibility="collapsed") if cur_lineup_names else None
                                    available_in    = [p[1] for p in all_players_arena if p[0] not in all_match_player_ids]
                                    player_in_name  = _sc3.selectbox("Entra", available_in, key=f"sub_in_{m_id}", label_visibility="collapsed") if available_in else None
                                    sub_reason      = _sc4.text_input("Motivo", key=f"sub_reason_{m_id}", label_visibility="collapsed", placeholder="Motivo de sustitución")
                                    if _sc5.button("✅", key=f"sub_submit_{m_id}", use_container_width=True) and player_in_name and sub_reason:
                                        p_out_row = conn.execute("SELECT id FROM players WHERE name=?", (player_out_name,)).fetchone()
                                        p_in_row  = conn.execute(f"SELECT id, name, {mc_a}, pts_{t_cat}, {rc_a} FROM players WHERE name=?", (player_in_name,)).fetchone()
                                        if p_out_row and p_in_row:
                                            p_in_snap = json.dumps({"gamertag": p_in_row[1], "mmr": round(p_in_row[2], 2), "pr": int(p_in_row[3]), "rank": p_in_row[4]})
                                            conn.execute(
                                                "INSERT INTO player_substitutions (match_id,tournament_id,team_id,player_out_id,player_in_id,reason,substituted_at,player_in_snapshot) VALUES (?,?,?,?,?,?,?,?)",
                                                (m_id, t_id, sub_team_id, p_out_row[0], p_in_row[0], sub_reason, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), p_in_snap)
                                            )
                                            conn.execute("""
                                                UPDATE match_lineups SET player_id=?
                                                WHERE team_id=? AND player_id=?
                                                  AND match_id IN (
                                                    SELECT id FROM matches WHERE tournament_id=?
                                                      AND status NOT IN ('COMPLETED','Completed','BYE_COMPLETED')
                                                  )
                                            """, (p_in_row[0], sub_team_id, p_out_row[0], t_id))
                                            conn.commit()
                                            for _k in [f"sub_team_{m_id}", f"sub_out_{m_id}", f"sub_in_{m_id}", f"sub_reason_{m_id}"]:
                                                st.session_state.pop(_k, None)
                                            st.session_state["toast_msg"] = f"Sustitución: {player_out_name} → {player_in_name}."
                                            st.rerun()
            with tab_all:
                _all_btypes = [("UPPER","⬆️ Upper Bracket","#0284c7")]
                if has_lb:
                    _all_btypes += [("LOWER","⬇️ Lower Bracket","#7c3aed"),("GRAND_FINAL","🏆 Gran Final","#d97706")]
                for _abt,_albl,_acc in _all_btypes:
                    _abm = conn.execute("""
                        SELECT m.id,m.round_name,t1.team_name,t2.team_name,
                               m.status,m.team1_id,m.team2_id,m.winner_id,m.is_bracket_reset,
                               m.round_number,m.position
                        FROM matches m
                        LEFT JOIN teams t1 ON m.team1_id=t1.id
                        LEFT JOIN teams t2 ON m.team2_id=t2.id
                        WHERE m.tournament_id=? AND m.bracket_type=?
                        ORDER BY m.round_number ASC,m.position ASC
                    """, (t_id,_abt)).fetchall()
                    if _abm:
                        st.markdown(f"<div style='font-size:0.72rem;font-weight:800;letter-spacing:0.1em;text-transform:uppercase;color:{_acc};margin:14px 0 4px;'>{_albl}</div>", unsafe_allow_html=True)
                        st.markdown(_render_bracket_svg(_abm, accent=_acc), unsafe_allow_html=True)
            _not_done = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE tournament_id=? AND status IN ('READY','IN_PROGRESS')", (t_id,)
            ).fetchone()[0]
            _done_ct = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE tournament_id=? AND status IN ('COMPLETED','BYE_COMPLETED')", (t_id,)
            ).fetchone()[0]
            if _not_done == 0 and _done_ct > 0:
                _champ = conn.execute(
                    "SELECT tw.team_name FROM matches m JOIN teams tw ON m.winner_id=tw.id WHERE m.tournament_id=? AND m.status='COMPLETED' ORDER BY m.round_number DESC,m.id DESC LIMIT 1",
                    (t_id,)
                ).fetchone()
                st.markdown(f"<div style='background:linear-gradient(135deg,rgba(251,191,36,0.12),rgba(245,158,11,0.04));border:1px solid rgba(251,191,36,0.35);border-left:5px solid #fbbf24;border-radius:10px;padding:16px 20px;margin:16px 0 8px;text-align:center;'><div style='font-size:1.6rem;margin-bottom:4px;'>🏆</div><div style='font-size:1.1rem;font-weight:800;color:#fbbf24;'>{_champ[0] if _champ else '—'}</div><div style='font-size:0.72rem;color:#94a3b8;margin-top:3px;letter-spacing:0.08em;'>CAMPEÓN DEL TORNEO</div></div>", unsafe_allow_html=True)
                if st.button("🏆 Terminar Torneo", key=f"btn_end_{t_id}", type="primary", use_container_width=True):
                    conn.execute("UPDATE tournaments SET status='Completed' WHERE id=?", (t_id,))
                    conn.commit()
                    conn.close()
                    st.session_state["toast_msg"] = "¡Torneo finalizado! 🏆"
                    st.rerun()
            conn.close()

# ---------------------------------------------------------
# SECCIÓN 4: HISTÓRICO & LLAVES
# ---------------------------------------------------------
elif menu == "📊 Histórico & Llaves":
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    tournaments = conn.execute("SELECT id,name,created_by,category,format,date,status FROM tournaments ORDER BY id DESC").fetchall()
    if not tournaments:
        st.info("No hay torneos registrados todavía.")
        conn.close()
    else:
        def _t_label(t):
            icon = "🟢" if t[6]=="In Progress" else "🏆" if t[6]=="Completed" else "⚪"
            return f"{icon}  #{t[0]} — {t[1]}  ({t[5][:10]})"
        t_options = {_t_label(t): t[0] for t in tournaments}
        _preset_tid = st.session_state.pop("_hist_preset_tid", None)
        _t_ids = list(t_options.values())
        _default_idx = _t_ids.index(_preset_tid) if _preset_tid and _preset_tid in _t_ids else 0
        sel  = st.selectbox("Seleccionar Torneo", list(t_options.keys()), index=_default_idx)
        t_id = t_options[sel]
        t_data = next(t for t in tournaments if t[0]==t_id)
        _,t_name,t_org,t_cat,t_fmt,t_date,t_status = t_data

        sc = "#22c55e" if t_status=="Completed" else "#f59e0b" if t_status=="In Progress" else "#94a3b8"
        sl = "Completado" if t_status=="Completed" else "En Curso" if t_status=="In Progress" else "Pendiente"

        is_old_fmt = conn.execute("SELECT COUNT(*) FROM matches WHERE tournament_id=? AND round_name='Primera Ronda'", (t_id,)).fetchone()[0] > 0

        champion = None
        if t_status == "Completed":
            if is_old_fmt:
                champion = conn.execute("SELECT tw.team_name FROM matches m JOIN teams tw ON m.winner_id=tw.id WHERE m.tournament_id=? AND m.round_name='GRAN FINAL' AND m.winner_id IS NOT NULL", (t_id,)).fetchone()
            else:
                champion = conn.execute("SELECT tw.team_name FROM matches m JOIN teams tw ON m.winner_id=tw.id WHERE m.tournament_id=? AND m.bracket_type='GRAND_FINAL' AND m.status='COMPLETED' ORDER BY m.round_number DESC LIMIT 1", (t_id,)).fetchone()
                if not champion:
                    champion = conn.execute("SELECT tw.team_name FROM matches m JOIN teams tw ON m.winner_id=tw.id WHERE m.tournament_id=? AND m.bracket_type='UPPER' AND m.status='COMPLETED' ORDER BY m.round_number DESC LIMIT 1", (t_id,)).fetchone()

        if champion:
            st.markdown(f"""
            <div style='background:linear-gradient(135deg,rgba(251,191,36,0.1),rgba(245,158,11,0.04));
                 border:1px solid rgba(251,191,36,0.3);border-left:5px solid #fbbf24;
                 border-radius:10px;padding:20px 24px;margin-bottom:16px;'>
                <div style='text-align:center;padding-bottom:14px;
                     border-bottom:1px solid rgba(251,191,36,0.18);margin-bottom:14px;'>
                    <div style='font-size:1.6rem;margin-bottom:4px;'>🏆</div>
                    <div style='font-size:1.25rem;font-weight:800;color:#fbbf24;'>{champion[0]}</div>
                    <div style='font-size:0.72rem;color:#94a3b8;margin-top:3px;letter-spacing:0.08em;'>CAMPEÓN DEL TORNEO</div>
                </div>
                <div style='display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;'>
                    <div style='display:flex;gap:16px;flex-wrap:wrap;'>
                        <span style='color:#94a3b8;font-size:0.78rem;'>🗓️ {t_date[:10]}</span>
                        <span style='color:#94a3b8;font-size:0.78rem;'>🎮 {t_cat}</span>
                        <span style='color:#94a3b8;font-size:0.78rem;'>📋 {t_fmt}</span>
                        <span style='color:#94a3b8;font-size:0.78rem;'>👤 {t_org}</span>
                    </div>
                    <span style='background:rgba(34,197,94,0.18);color:#22c55e;padding:3px 12px;
                          border-radius:12px;font-size:0.75rem;font-weight:700;'>Completado</span>
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div style='border:1px solid {sc}33;border-left:5px solid {sc};border-radius:10px;
                 padding:16px 24px;margin-bottom:16px;background:{sc}08;'>
                <div style='display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;'>
                    <div>
                        <span style='font-size:1.35rem;font-weight:800;color:#0f172a;'>{t_name}</span>
                        <span style='background:{sc}22;color:{sc};padding:3px 12px;border-radius:12px;
                              font-size:0.8rem;font-weight:700;margin-left:12px;'>{sl}</span>
                    </div>
                    <div style='color:#94a3b8;font-size:0.8rem;display:flex;gap:14px;flex-wrap:wrap;'>
                        <span>🗓️ {t_date[:10]}</span>
                        <span>🎮 {t_cat}</span>
                        <span>📋 {t_fmt}</span>
                        <span>👤 {t_org}</span>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        n_teams  = conn.execute("SELECT COUNT(*) FROM teams WHERE tournament_id=?", (t_id,)).fetchone()[0]
        n_played = conn.execute("SELECT COUNT(*) FROM matches WHERE tournament_id=? AND status IN ('COMPLETED','Completed','BYE_COMPLETED')", (t_id,)).fetchone()[0]
        n_total  = conn.execute("SELECT COUNT(*) FROM matches WHERE tournament_id=?", (t_id,)).fetchone()[0]
        _sc1, _sc2, _sc3 = st.columns(3)
        for _col, _val, _lbl, _clr in [
            (_sc1, n_teams,  "Equipos",          "#3b82f6"),
            (_sc2, n_played, "Partidos jugados",  "#22c55e"),
            (_sc3, n_total,  "Partidos totales",  "#8b5cf6"),
        ]:
            _col.markdown(f"""
            <div style='border:1px solid {_clr}33;border-radius:10px;padding:14px 16px;
                 background:{_clr}06;text-align:center;margin-bottom:12px;'>
                <div style='font-size:1.8rem;font-weight:800;color:{_clr};line-height:1;'>{_val}</div>
                <div style='font-size:0.73rem;color:#94a3b8;margin-top:4px;'>{_lbl}</div>
            </div>
            """, unsafe_allow_html=True)

        def _round_header(rn):
            st.markdown(f"""
            <div style='border-left:3px solid #0284c7;padding:5px 12px;margin:16px 0 8px 0;
                 background:rgba(2,132,199,0.05);border-radius:0 6px 6px 0;'>
                <span style='font-size:0.82rem;font-weight:700;color:#0284c7;
                      text-transform:uppercase;letter-spacing:0.07em;'>{rn}</span>
            </div>
            """, unsafe_allow_html=True)

        def _render_h_match(m_id, t1n, t2n, st_val, t1i, t2i, win_id):
            t1_disp = t1n or "Por definir"
            t2_disp = t2n or "Por definir"
            pl1 = conn.execute("SELECT DISTINCT p.name FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (m_id, t1i)).fetchall() if t1i else []
            pl2 = conn.execute("SELECT DISTINCT p.name FROM match_lineups ml JOIN players p ON ml.player_id=p.id WHERE ml.match_id=? AND ml.team_id=?", (m_id, t2i)).fetchall() if t2i else []
            p1 = " · ".join(p[0] for p in pl1) or "—"
            p2 = " · ".join(p[0] for p in pl2) or "—"
            done = st_val in ('COMPLETED', 'Completed', 'BYE_COMPLETED')
            t1w  = done and win_id == t1i
            t2w  = done and win_id == t2i
            s1   = "color:#22c55e;font-weight:700" if t1w else ("color:#64748b" if done else "color:#e2e8f0;font-weight:600")
            s2   = "color:#22c55e;font-weight:700" if t2w else ("color:#64748b" if done else "color:#e2e8f0;font-weight:600")
            lborder = "#22c55e" if (done and st_val != 'BYE_COMPLETED') else ("#94a3b8" if st_val == 'BYE_COMPLETED' else ("#0284c7" if st_val == 'READY' else "#1e293b"))
            if   st_val == 'PENDING':       badge = "<span style='background:rgba(71,85,105,0.2);color:#94a3b8;padding:2px 9px;border-radius:6px;font-size:0.7rem;font-weight:700;'>Pendiente</span>"
            elif st_val == 'READY':         badge = "<span style='background:rgba(2,132,199,0.15);color:#0284c7;padding:2px 9px;border-radius:6px;font-size:0.7rem;font-weight:700;'>Listo</span>"
            elif st_val == 'IN_PROGRESS':   badge = "<span style='background:rgba(249,115,22,0.15);color:#f97316;padding:2px 9px;border-radius:6px;font-size:0.7rem;font-weight:700;'>En Juego</span>"
            elif st_val == 'BYE_COMPLETED': badge = "<span style='background:rgba(148,163,184,0.15);color:#94a3b8;padding:2px 9px;border-radius:6px;font-size:0.7rem;font-weight:700;'>BYE</span>"
            else:                           badge = "<span style='background:rgba(34,197,94,0.15);color:#22c55e;padding:2px 9px;border-radius:6px;font-size:0.7rem;font-weight:700;'>Jugado</span>"
            st.markdown(f"""
            <div style='border:1px solid #1e293b;border-left:3px solid {lborder};border-radius:8px;
                 padding:12px 16px;margin-bottom:6px;display:flex;align-items:center;gap:14px;'>
                <div style='flex:1;text-align:right;'>
                    <div style='{s1};font-size:0.95rem;'>{"🏆 " if t1w else ""}{t1_disp}</div>
                    <div style='color:#94a3b8;font-size:0.72rem;margin-top:3px;'>{p1}</div>
                </div>
                <div style='text-align:center;min-width:68px;'>
                    <div style='color:#64748b;font-size:0.72rem;font-weight:800;letter-spacing:0.1em;margin-bottom:5px;'>VS</div>
                    {badge}
                </div>
                <div style='flex:1;text-align:left;'>
                    <div style='{s2};font-size:0.95rem;'>{"🏆 " if t2w else ""}{t2_disp}</div>
                    <div style='color:#94a3b8;font-size:0.72rem;margin-top:3px;'>{p2}</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        if is_old_fmt:
            matches_h = conn.execute("SELECT m.id,m.round_name,t1.team_name,t2.team_name,m.status,m.team1_id,m.team2_id,m.winner_id FROM matches m LEFT JOIN teams t1 ON m.team1_id=t1.id LEFT JOIN teams t2 ON m.team2_id=t2.id WHERE m.tournament_id=? ORDER BY m.id ASC", (t_id,)).fetchall()
            rondas_h = {}
            for m in matches_h: rondas_h.setdefault(m[1], []).append(m)
            for rn, rlist in rondas_h.items():
                _round_header(rn)
                for m in rlist: _render_h_match(m[0], m[2], m[3], m[4], m[5], m[6], m[7])
        else:
            has_lb  = conn.execute("SELECT COUNT(*) FROM matches WHERE tournament_id=? AND bracket_type='LOWER'", (t_id,)).fetchone()[0] > 0
            teams_h = conn.execute("SELECT id,team_name,total_points FROM teams WHERE tournament_id=? ORDER BY total_points DESC", (t_id,)).fetchall()
            if has_lb:
                tab_ub, tab_lb, tab_gf, tab_eq, tab_all = st.tabs(["Upper Bracket", "Lower Bracket", "Gran Final", "Equipos", "📊 Completo"])
                bracket_secs = [("UPPER", tab_ub), ("LOWER", tab_lb), ("GRAND_FINAL", tab_gf)]
            else:
                tab_ub, tab_eq, tab_all = st.tabs(["Bracket", "Equipos", "📊 Completo"])
                bracket_secs = [("UPPER", tab_ub)]
            for btype, ctx in bracket_secs:
                with ctx:
                    _svg_bm = conn.execute("""
                        SELECT m.id,m.round_name,t1.team_name,t2.team_name,
                               m.status,m.team1_id,m.team2_id,m.winner_id,m.is_bracket_reset,
                               m.round_number,m.position
                        FROM matches m
                        LEFT JOIN teams t1 ON m.team1_id=t1.id
                        LEFT JOIN teams t2 ON m.team2_id=t2.id
                        WHERE m.tournament_id=? AND m.bracket_type=?
                        ORDER BY m.round_number ASC,m.position ASC
                    """, (t_id, btype)).fetchall()
                    if _svg_bm:
                        st.markdown(_render_bracket_svg(_svg_bm, accent="#0284c7"), unsafe_allow_html=True)
                    bm_list = conn.execute("SELECT m.id,m.round_name,t1.team_name,t2.team_name,m.status,m.team1_id,m.team2_id,m.winner_id,m.is_bracket_reset FROM matches m LEFT JOIN teams t1 ON m.team1_id=t1.id LEFT JOIN teams t2 ON m.team2_id=t2.id WHERE m.tournament_id=? AND m.bracket_type=? ORDER BY m.round_number ASC,m.position ASC", (t_id, btype)).fetchall()
                    cur_rnd = None
                    for bm in bm_list:
                        m_id, rn, t1n, t2n, st_val, t1i, t2i, win_id, is_br = bm
                        if is_br == 1 and st_val == 'PENDING':
                            continue
                        if rn != cur_rnd:
                            cur_rnd = rn
                            _round_header(rn)
                        _render_h_match(m_id, t1n, t2n, st_val, t1i, t2i, win_id)
            with tab_eq:
                # Mismos colores sólidos del componente DnD (fondo color + texto blanco)
                _rank_clrs = {
                    "D":    "#475569", "D+":   "#57534e",
                    "C":    "#059669", "C+":   "#047857",
                    "B":    "#2563eb", "B+":   "#1d4ed8",
                    "A":    "#7c3aed", "A+":   "#6d28d9",
                    "S":    "#d97706", "S+":   "#b45309",
                    "S++":  "#dc2626", "S+++": "#b91c1c",
                }
                _p_medal_colors = ["#d97706", "#64748b", "#92400e"]
                _p_medal_labels = ["1er Lugar", "2do Lugar", "3er Lugar"]

                def _team_card_html(t, mc, ml, mt="0px"):
                    players_t = conn.execute(
                        "SELECT DISTINCT p.name, p.rank_category FROM match_lineups ml "
                        "JOIN players p ON ml.player_id=p.id WHERE ml.team_id=?", (t[0],)
                    ).fetchall()
                    rows = ""
                    for p in players_t:
                        rc = _rank_clrs.get(p[1], "#475569")
                        rows += (
                            f"<div style='background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:9px;"
                            f"padding:8px 10px;display:flex;align-items:center;"
                            f"justify-content:space-between;margin-bottom:4px;'>"
                            f"<span style='font-weight:700;font-size:0.93rem;color:#0f172a;'>{p[0]}</span>"
                            f"<span style='background:{rc};color:#fff;padding:2px 8px;"
                            f"border-radius:5px;font-size:0.72rem;font-weight:800;'>{p[1]}</span>"
                            f"</div>"
                        )
                    return (
                        f"<div style='background:#ffffff;border:1.5px solid #e2e8f0;border-top:4px solid {mc};"
                        f"border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.05);margin-top:{mt};'>"
                        f"<div style='padding:9px 12px 8px;border-bottom:1px solid #f1f5f9;'>"
                        f"<div style='font-size:0.7rem;font-weight:800;letter-spacing:0.07em;text-transform:uppercase;"
                        f"color:{mc};margin-bottom:4px;'>{t[1]}</div>"
                        f"<span style='background:{mc};color:#fff;padding:2px 9px;"
                        f"border-radius:5px;font-size:0.7rem;font-weight:700;'>{ml}</span>"
                        f"<div style='font-size:0.67rem;color:#94a3b8;margin-top:3px;'>MMR: {round(t[2],0):.0f}</div>"
                        f"</div>"
                        f"<div style='padding:8px 10px;'>{rows}</div>"
                        f"</div>"
                    )

                n_t = len(teams_h)
                if n_t == 0:
                    st.info("No hay equipos registrados.")
                elif n_t == 1:
                    _solo, _, _, _ = st.columns(4)
                    _solo.markdown(_team_card_html(teams_h[0], _p_medal_colors[0], _p_medal_labels[0]), unsafe_allow_html=True)
                elif n_t == 2:
                    _ca, _cb, _, _ = st.columns(4)
                    _ca.markdown(_team_card_html(teams_h[0], _p_medal_colors[0], _p_medal_labels[0], "0px"),  unsafe_allow_html=True)
                    _cb.markdown(_team_card_html(teams_h[1], _p_medal_colors[1], _p_medal_labels[1], "50px"), unsafe_allow_html=True)
                else:
                    # Podium top 3: 2do | 1ro | 3ro con escalonado visual
                    _cl, _cc, _cr = st.columns(3)
                    _cc.markdown(_team_card_html(teams_h[0], _p_medal_colors[0], _p_medal_labels[0], "0px"),  unsafe_allow_html=True)
                    _cl.markdown(_team_card_html(teams_h[1], _p_medal_colors[1], _p_medal_labels[1], "50px"), unsafe_allow_html=True)
                    _cr.markdown(_team_card_html(teams_h[2], _p_medal_colors[2], _p_medal_labels[2], "90px"), unsafe_allow_html=True)
                    # 4to en adelante: siempre 4 columnas fijas por fila
                    if n_t > 3:
                        st.markdown("<div style='border-top:1px solid #e2e8f0;margin-top:24px;padding-top:16px;'></div>", unsafe_allow_html=True)
                        _rem = teams_h[3:]
                        for _row_start in range(0, len(_rem), 4):
                            _row = _rem[_row_start:_row_start + 4]
                            _rcols = st.columns(4)
                            for _ri, _rt in enumerate(_row):
                                _rcols[_ri].markdown(
                                    _team_card_html(_rt, "#94a3b8", f"#{_row_start + _ri + 4}", "0px"),
                                    unsafe_allow_html=True
                                )
            with tab_all:
                _all_btypes_h = [("UPPER","Upper Bracket","#0284c7")]
                if has_lb:
                    _all_btypes_h += [("LOWER","Lower Bracket","#7c3aed"),("GRAND_FINAL","Gran Final","#d97706")]
                _svg_sections = []
                for _abt,_albl,_acc in _all_btypes_h:
                    _abm = conn.execute("""
                        SELECT m.id,m.round_name,t1.team_name,t2.team_name,
                               m.status,m.team1_id,m.team2_id,m.winner_id,m.is_bracket_reset,
                               m.round_number,m.position
                        FROM matches m
                        LEFT JOIN teams t1 ON m.team1_id=t1.id
                        LEFT JOIN teams t2 ON m.team2_id=t2.id
                        WHERE m.tournament_id=? AND m.bracket_type=?
                        ORDER BY m.round_number ASC,m.position ASC
                    """, (t_id,_abt)).fetchall()
                    if _abm:
                        _svg_sections.append((_albl, _acc, _abm))
                if _svg_sections:
                    _combined = _build_combined_svg(_svg_sections)
                    st.markdown(
                        f"<div style='overflow:auto;background:#f8fafc;border:1.5px solid #e2e8f0;"
                        f"border-radius:12px;padding:12px;margin-bottom:10px;'>{_combined}</div>",
                        unsafe_allow_html=True
                    )
                    st.download_button(
                        label="⬇️ Descargar SVG",
                        data=_combined.encode("utf-8"),
                        file_name=f"brackets_torneo_{t_id}.svg",
                        mime="image/svg+xml",
                        use_container_width=True,
                    )
                else:
                    st.info("No hay partidas registradas aún.")
        conn.close()

# ---------------------------------------------------------
# SECCIÓN 5: PERFIL DE JUGADOR
# ---------------------------------------------------------
elif menu == "👤 Perfil de Jugador":
    st.header("👤 Perfil Competitivo")
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    players = conn.execute("SELECT id, name FROM players ORDER BY name ASC").fetchall()

    if not players:
        st.info("No hay jugadores registrados.")
        conn.close()
    else:
        p_dict = {p[1]: p[0] for p in players}
        sel_p  = st.selectbox("Buscar Jugador", list(p_dict.keys()))
        p_id   = p_dict[sel_p]

        # ── Compute stats per mode ────────────────────────────────────────────
        mode_stats = {}
        for mode, (mc, rc, pc) in MODE_COLS.items():
            p_data = conn.execute(f"SELECT {mc},{rc},{pc} FROM players WHERE id=?", (p_id,)).fetchone()

            wins = conn.execute("""
                SELECT COUNT(DISTINCT m.id) FROM match_lineups ml
                JOIN matches m ON ml.match_id=m.id
                JOIN tournaments t ON m.tournament_id=t.id
                WHERE ml.player_id=? AND ml.team_id=m.winner_id
                  AND m.status IN ('COMPLETED','Completed') AND t.category=?
            """, (p_id, mode)).fetchone()[0]

            losses = conn.execute("""
                SELECT COUNT(DISTINCT m.id) FROM match_lineups ml
                JOIN matches m ON ml.match_id=m.id
                JOIN tournaments t ON m.tournament_id=t.id
                WHERE ml.player_id=? AND m.winner_id IS NOT NULL
                  AND ml.team_id != m.winner_id
                  AND (ml.team_id=m.team1_id OR ml.team_id=m.team2_id)
                  AND m.status IN ('COMPLETED','Completed') AND t.category=?
            """, (p_id, mode)).fetchone()[0]

            tourn_played = conn.execute("""
                SELECT COUNT(DISTINCT t.id) FROM match_lineups ml
                JOIN matches m ON ml.match_id=m.id
                JOIN tournaments t ON m.tournament_id=t.id
                WHERE ml.player_id=? AND t.category=?
            """, (p_id, mode)).fetchone()[0]

            tourn_won = conn.execute("""
                SELECT COUNT(DISTINCT t.id) FROM tournaments t
                JOIN matches m ON m.tournament_id=t.id
                JOIN match_lineups ml ON ml.match_id=m.id
                WHERE ml.player_id=? AND t.category=? AND t.status='Completed'
                  AND ml.team_id=m.winner_id
                  AND (m.bracket_type='GRAND_FINAL' OR m.round_name='GRAN FINAL')
                  AND m.status IN ('COMPLETED','Completed')
            """, (p_id, mode)).fetchone()[0]

            total   = wins + losses
            winrate = round(wins / total * 100, 1) if total > 0 else 0.0
            mode_stats[mode] = {
                "mmr": p_data[0], "rank": p_data[1], "pr": int(p_data[2]),
                "wins": wins, "losses": losses, "total": total, "winrate": winrate,
                "tourn_played": tourn_played, "tourn_won": tourn_won
            }

        # ── Player banner ─────────────────────────────────────────────────────
        best_s = max(mode_stats.values(), key=lambda s: s["pr"])
        _rank_colors = {
            "D": "#64748b", "D+": "#64748b", "C": "#10b981", "C+": "#10b981",
            "B": "#3b82f6", "B+": "#3b82f6", "A": "#8b5cf6", "A+": "#8b5cf6",
            "S": "#f59e0b", "S+": "#f59e0b", "S++": "#ef4444", "S+++": "#ef4444"
        }
        rc_color = _rank_colors.get(best_s["rank"], "#64748b")
        st.markdown(f"""
        <div style='border:1px solid {rc_color}33;border-left:5px solid {rc_color};border-radius:10px;
             padding:16px 24px;margin-bottom:16px;background:{rc_color}08;'>
            <span style='font-size:1.6rem;font-weight:800;'>{sel_p}</span>
            <span style='background:{rc_color}22;color:{rc_color};padding:3px 12px;border-radius:12px;
                  font-size:0.9rem;font-weight:700;margin-left:14px;'>{best_s["rank"]}</span>
            <span style='color:#64748b;font-size:0.85rem;margin-left:14px;'>
                PR máx: {best_s["pr"]} · MMR: {round(best_s["mmr"], 2)}
            </span>
        </div>
        """, unsafe_allow_html=True)

        tab_stats, tab_matches, tab_tournaments, tab_pr = st.tabs([
            "📊 Estadísticas", "📋 Historial de Partidas", "🏆 Torneos", "📈 Evolución PR"
        ])

        # ── TAB 1: STATISTICS ─────────────────────────────────────────────────
        with tab_stats:
            st.subheader("📊 Rendimiento por Modalidad")
            _mode_palette = {"4v4": ("🔵", "#3b82f6"), "2v2": ("🟢", "#22c55e"), "1v1": ("🟡", "#f59e0b")}
            cols_s = st.columns(3)
            for i, (mode, (icon, color)) in enumerate(_mode_palette.items()):
                s = mode_stats[mode]
                with cols_s[i]:
                    st.markdown(f"""
                    <div style='border:1px solid {color}33;border-radius:10px;padding:16px;background:{color}06;margin-bottom:8px;'>
                        <div style='font-size:1.1rem;font-weight:800;margin-bottom:12px;'>{icon} {mode}</div>
                        <div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;'>
                            <div style='text-align:center;background:{color}11;border-radius:8px;padding:8px;'>
                                <div style='font-size:1.4rem;font-weight:800;color:{color};'>{s["rank"]}</div>
                                <div style='font-size:0.72rem;color:#64748b;'>Rango</div>
                            </div>
                            <div style='text-align:center;background:{color}11;border-radius:8px;padding:8px;'>
                                <div style='font-size:1.4rem;font-weight:800;color:{color};'>{s["pr"]}</div>
                                <div style='font-size:0.72rem;color:#64748b;'>PR</div>
                            </div>
                            <div style='text-align:center;background:#22c55e11;border-radius:8px;padding:8px;'>
                                <div style='font-size:1.3rem;font-weight:700;color:#22c55e;'>{s["wins"]}</div>
                                <div style='font-size:0.72rem;color:#64748b;'>Victorias</div>
                            </div>
                            <div style='text-align:center;background:#ef444411;border-radius:8px;padding:8px;'>
                                <div style='font-size:1.3rem;font-weight:700;color:#ef4444;'>{s["losses"]}</div>
                                <div style='font-size:0.72rem;color:#64748b;'>Derrotas</div>
                            </div>
                        </div>
                        <div style='margin-top:10px;background:#1e293b;border-radius:6px;height:8px;overflow:hidden;'>
                            <div style='background:{color};height:100%;width:{s["winrate"]}%;border-radius:6px;'></div>
                        </div>
                        <div style='display:flex;justify-content:space-between;margin-top:4px;font-size:0.78rem;color:#64748b;'>
                            <span>{s["total"]} partidas</span>
                            <span style='color:{color};font-weight:700;'>{s["winrate"]}% WR</span>
                        </div>
                        <div style='margin-top:10px;border-top:1px solid #1e293b;padding-top:10px;
                             display:flex;justify-content:space-between;font-size:0.82rem;'>
                            <span style='color:#94a3b8;'>Torneos: <b style='color:#e2e8f0;'>{s["tourn_played"]}</b></span>
                            <span style='color:#fbbf24;'>🥇 Ganados: <b>{s["tourn_won"]}</b></span>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

        # ── TAB 2: MATCH HISTORY ──────────────────────────────────────────────
        with tab_matches:
            st.subheader(f"📋 Historial de Partidas — {sel_p}")
            match_hist = conn.execute("""
                SELECT DISTINCT
                    m.id, t.name, t.category, t.date,
                    m.round_name, ml.team_id, m.winner_id,
                    m.score, m.final_result_snapshot,
                    mt.team_name,
                    m.team1_id, m.team2_id,
                    t1n.team_name, t2n.team_name
                FROM match_lineups ml
                JOIN matches m ON ml.match_id=m.id
                JOIN tournaments t ON m.tournament_id=t.id
                LEFT JOIN teams mt  ON mt.id=ml.team_id
                LEFT JOIN teams t1n ON t1n.id=m.team1_id
                LEFT JOIN teams t2n ON t2n.id=m.team2_id
                WHERE ml.player_id=?
                  AND m.status IN ('COMPLETED','Completed')
                ORDER BY t.date DESC, m.id DESC
            """, (p_id,)).fetchall()

            if not match_hist:
                st.info("Este jugador no tiene partidas completadas registradas.")
            else:
                st.caption(f"{len(match_hist)} partidas encontradas")
                for row in match_hist:
                    (m_id, t_name, t_cat, t_date, rn, my_tid, win_tid,
                     m_score, snap_str, my_team, t1_id, t2_id, t1_n, t2_n) = row
                    opp_team = t2_n if my_tid == t1_id else t1_n
                    won = my_tid == win_tid and win_tid is not None
                    res_color = "#22c55e" if won else "#ef4444"
                    res_label = "VICTORIA" if won else "DERROTA"

                    pr_delta_str = "—"
                    if snap_str:
                        try:
                            for pr_r in json.loads(snap_str).get("playerResults", []):
                                if str(pr_r.get("playerId", "")) == str(p_id):
                                    d = pr_r.get("prChange", 0)
                                    pr_delta_str = f"{'+'if d>=0 else ''}{d} PR"
                                    break
                        except Exception:
                            pass

                    st.markdown(f"""
                    <div style='border:1px solid #1e293b;border-left:4px solid {res_color};border-radius:8px;
                         padding:10px 16px;margin-bottom:6px;display:flex;align-items:center;gap:12px;'>
                        <span style='background:{res_color}22;color:{res_color};padding:2px 10px;border-radius:8px;
                              font-size:0.75rem;font-weight:800;min-width:70px;text-align:center;'>{res_label}</span>
                        <div style='flex:1;'>
                            <div style='font-weight:600;font-size:0.9rem;'>
                                {my_team or "Mi Equipo"} vs {opp_team or "—"}
                            </div>
                            <div style='color:#64748b;font-size:0.75rem;'>{t_name} · {rn} · {t_cat}</div>
                        </div>
                        <div style='text-align:right;'>
                            <div style='color:{res_color};font-weight:700;font-size:0.9rem;'>{pr_delta_str}</div>
                            <div style='color:#475569;font-size:0.72rem;'>
                                {t_date[:10] if t_date else "—"} · {m_score or "—"}
                            </div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

        # ── TAB 3: TOURNAMENT HISTORY ─────────────────────────────────────────
        with tab_tournaments:
            st.subheader(f"🏆 Historial de Torneos — {sel_p}")
            tourn_hist = conn.execute("""
                SELECT DISTINCT t.id, t.name, t.category, t.format, t.date, t.status
                FROM tournaments t
                JOIN teams tm ON tm.tournament_id=t.id
                JOIN match_lineups ml ON ml.team_id=tm.id
                WHERE ml.player_id=?
                ORDER BY t.date DESC
            """, (p_id,)).fetchall()

            if not tourn_hist:
                st.info("Este jugador no ha participado en torneos.")
            else:
                st.caption(f"{len(tourn_hist)} torneos encontrados")
                for (t_id_h, t_name, t_cat, t_fmt, t_date, t_status) in tourn_hist:
                    player_team = conn.execute("""
                        SELECT DISTINCT tm.team_name, tm.id FROM teams tm
                        JOIN match_lineups ml ON ml.team_id=tm.id
                        WHERE tm.tournament_id=? AND ml.player_id=? LIMIT 1
                    """, (t_id_h, p_id)).fetchone()

                    phase = "Participó"
                    if player_team:
                        last_m = conn.execute("""
                            SELECT m.round_name FROM matches m
                            WHERE m.tournament_id=? AND (m.team1_id=? OR m.team2_id=?)
                              AND m.status IN ('COMPLETED','Completed')
                            ORDER BY m.round_number DESC, m.id DESC LIMIT 1
                        """, (t_id_h, player_team[1], player_team[1])).fetchone()
                        if last_m:
                            phase = last_m[0]

                    won_t = conn.execute("""
                        SELECT 1 FROM matches m
                        JOIN match_lineups ml ON ml.match_id=m.id
                        WHERE m.tournament_id=? AND ml.player_id=?
                          AND ml.team_id=m.winner_id
                          AND (m.bracket_type='GRAND_FINAL' OR m.round_name='GRAN FINAL')
                          AND m.status IN ('COMPLETED','Completed')
                    """, (t_id_h, p_id)).fetchone()

                    champ = won_t is not None
                    # Determinar resultado si el torneo terminó
                    if t_status == "Completed":
                        res_label = "🥇 CAMPEÓN" if champ else "❌ ELIMINADO"
                        res_color = "#fbbf24" if champ else "#ef4444"
                    else:
                        res_label = "🔄 EN CURSO"
                        res_color = "#f59e0b"

                    team_label = f" · {player_team[0]}" if player_team else ""
                    left_col, btn_col = st.columns([5, 1])
                    with left_col:
                        st.markdown(f"""
                        <div style='border:1px solid #1e293b;border-radius:8px;padding:12px 16px;
                             border-left:4px solid {res_color};background:{res_color}06;'>
                            <div style='display:flex;justify-content:space-between;align-items:center;'>
                                <div>
                                    <span style='font-weight:700;font-size:0.95rem;'>{t_name}</span>
                                    <span style='background:{res_color}22;color:{res_color};padding:2px 8px;
                                          border-radius:8px;font-size:0.72rem;font-weight:700;margin-left:8px;'>{res_label}</span>
                                    <div style='color:#64748b;font-size:0.78rem;margin-top:4px;'>
                                        {t_cat} · {t_fmt} · 🗓️ {t_date[:10] if t_date else "—"}{team_label} · Hasta: {phase}
                                    </div>
                                </div>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                    with btn_col:
                        st.markdown("<div style='margin-top:8px;'>", unsafe_allow_html=True)
                        if st.button("Ver llave", key=f"nav_hist_{t_id_h}", use_container_width=True):
                            st.session_state["_nav_redirect"]   = "📊 Histórico & Llaves"
                            st.session_state["_hist_preset_tid"] = t_id_h
                            st.rerun()
                        st.markdown("</div>", unsafe_allow_html=True)

        # ── TAB 4: PR EVOLUTION ───────────────────────────────────────────────
        with tab_pr:
            st.subheader(f"📈 Evolución de PR — {sel_p}")

            hist_snaps = conn.execute("""
                SELECT m.id, t.category, t.date, m.final_result_snapshot
                FROM match_lineups ml
                JOIN matches m ON ml.match_id=m.id
                JOIN tournaments t ON m.tournament_id=t.id
                WHERE ml.player_id=? AND m.final_result_snapshot IS NOT NULL
                ORDER BY t.date ASC, m.id ASC
            """, (p_id,)).fetchall()

            pr_evo = {}
            for (m_id, t_cat, t_date, snap_str) in hist_snaps:
                try:
                    for pr_r in json.loads(snap_str).get("playerResults", []):
                        if str(pr_r.get("playerId", "")) == str(p_id):
                            label = f"{t_date[:10] if t_date else '?'}-#{m_id}"
                            pr_evo.setdefault(t_cat, []).append({
                                "label": label,
                                "pr": pr_r.get("prAfter", 0),
                                "change": pr_r.get("prChange", 0)
                            })
                            break
                except Exception:
                    pass

            if not pr_evo:
                st.info("No hay datos de evolución de PR. Se registran a partir de partidas con snapshot de resultado (requiere usar el botón ▶️ Iniciar Partida en Arena).")
            else:
                _ev_palette = {"4v4": ("🔵", "#3b82f6"), "2v2": ("🟢", "#22c55e"), "1v1": ("🟡", "#f59e0b")}
                for mode in ["4v4", "2v2", "1v1"]:
                    entries = pr_evo.get(mode)
                    if not entries:
                        continue
                    icon, color = _ev_palette[mode]
                    df_pr = pd.DataFrame({
                        "Partida": [e["label"] for e in entries],
                        f"PR {mode}": [e["pr"] for e in entries]
                    }).set_index("Partida")
                    st.markdown(f"**{icon} {mode}** — {len(entries)} registros · PR actual: **{entries[-1]['pr']}**")
                    st.line_chart(df_pr, height=200, color=color)
                    st.caption(
                        f"Máx: {max(e['pr'] for e in entries)} · "
                        f"Mín: {min(e['pr'] for e in entries)} · "
                        f"Δ acumulado: {sum(e['change'] for e in entries):+d} PR"
                    )
                    st.divider()

        conn.close()

# ---------------------------------------------------------
# SECCIÓN 6: PANEL DE ADMIN
# ---------------------------------------------------------
elif menu == "⚙️ Panel de Admin (MMR & Rangos)":
    st.header("⚙️ Configuración del Sistema Elo & Puntos")

    tab_elo, tab_ranks, tab_reset = st.tabs([
        "🎛️ Parámetros del Motor Elo y Puntos",
        "🏅 Configuración de Rangos",
        "🧹 Mantenimiento de Datos"
    ])

    with tab_elo:
        st.subheader("🎛️ Motor de PR por Modalidad")
        st.info(
            "**Sistema de Puntos de Rango (PR)**\n\n"
            "- **PR:** Puntos acumulados por partida. Nunca se reinician. "
            "El rango sube o baja según el PR acumulado.\n"
            "- **MMR:** Derivado automáticamente del PR (`floor(PR / 500) × 0.5`). "
            "Se usa para el balance de equipos en el draft.\n"
            "- **Rango:** Determinado por el MMR actual del jugador (thresholds fijos, no editables aquí).\n"
            "- **Multiplier:** A mayor diferencia de sumas MMR entre equipos, "
            "mayor recompensa por la sorpresa y menor por la victoria esperada."
        )
        mod_configs_admin = get_modality_configs()
        mod_sel = st.selectbox("Modalidad a configurar", ["4v4", "2v2", "1v1"], key="admin_mod_sel")
        cfg_edit = {k: v for k, v in mod_configs_admin.get(mod_sel, DEFAULT_MODALITY_CONFIGS[mod_sel]).items()}
        c1a, c2a, c3a = st.columns(3)
        with c1a:
            st.markdown("**PR Base**")
            cfg_edit["win_base_pr"]        = st.number_input("PR base por victoria",          1.0, 200.0, float(cfg_edit["win_base_pr"]),        1.0, key="adm_win_base")
            cfg_edit["loss_base_pr"]       = st.number_input("PR base por derrota",           1.0, 200.0, float(cfg_edit["loss_base_pr"]),       1.0, key="adm_loss_base")
            cfg_edit["sum_gap_multiplier"] = st.number_input("Multiplicador de brecha (K)",   0.1,  20.0, float(cfg_edit["sum_gap_multiplier"]), 0.5, key="adm_mult",
                help="Cuanto más alto, más sensible al desbalance. 4v4=3, 2v2=6, 1v1=10")
        with c2a:
            st.markdown("**Topes PR**")
            cfg_edit["max_pr_win"]  = st.number_input("PR máximo por victoria", 1.0, 500.0, float(cfg_edit["max_pr_win"]),  5.0, key="adm_max_win")
            cfg_edit["min_pr_win"]  = st.number_input("PR mínimo por victoria", 1.0, 100.0, float(cfg_edit["min_pr_win"]),  1.0, key="adm_min_win")
            cfg_edit["max_pr_loss"] = st.number_input("PR máximo por derrota",  1.0, 500.0, float(cfg_edit["max_pr_loss"]), 5.0, key="adm_max_loss")
            cfg_edit["min_pr_loss"] = st.number_input("PR mínimo por derrota",  1.0, 100.0, float(cfg_edit["min_pr_loss"]), 1.0, key="adm_min_loss")
        with c3a:
            st.markdown("**Ajustes Individuales**")
            cfg_edit["low_rank_threshold"]  = st.number_input("Umbral rango bajo (MMR ≤)",       0.5, 6.5, float(cfg_edit["low_rank_threshold"]),  0.5, key="adm_low_thr",
                help="Ganadores con MMR ≤ a este valor reciben bono extra")
            cfg_edit["low_rank_bonus"]      = st.number_input("Bono rango bajo (+PR)",            0.0, 50.0, float(cfg_edit["low_rank_bonus"]),      1.0, key="adm_low_bon")
            cfg_edit["high_rank_threshold"] = st.number_input("Umbral rango alto (MMR ≥)",       0.5, 6.5, float(cfg_edit["high_rank_threshold"]), 0.5, key="adm_high_thr",
                help="Ganadores con MMR ≥ a este valor reciben penalización anti-inflación")
            cfg_edit["high_rank_penalty"]   = st.number_input("Penalización rango alto (-PR)",   0.0, 50.0, float(cfg_edit["high_rank_penalty"]),   1.0, key="adm_high_pen")
        cfg_edit["pr_per_mmr_step"] = 500
        if st.button("Guardar Configuración de Motor PR", type="primary"):
            save_modality_config(mod_sel, cfg_edit)
            st.success(f"¡Configuración de {mod_sel} guardada!"); st.rerun()
        st.divider()
        st.markdown("**Tabla de referencia — PR por escenario con esta configuración:**")
        ex_data = []
        for gap_ex in [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]:
            wc = cfg_edit["win_base_pr"]  - gap_ex * cfg_edit["sum_gap_multiplier"]
            lc = cfg_edit["loss_base_pr"] + gap_ex * cfg_edit["sum_gap_multiplier"]
            ex_data.append({
                "Brecha (Δ MMR)": gap_ex,
                "+PR Ganador": round(max(cfg_edit["min_pr_win"],  min(cfg_edit["max_pr_win"],  wc))),
                "-PR Perdedor": round(max(cfg_edit["min_pr_loss"], min(cfg_edit["max_pr_loss"], lc))),
            })
        st.dataframe(pd.DataFrame(ex_data), hide_index=True, use_container_width=True)

    with tab_ranks:
        st.subheader("🏅 MMR mínimo por Rango")
        st.caption(
            "Cada valor indica el **MMR mínimo** para alcanzar ese rango. "
            "El PR equivalente se calcula automáticamente: `PR = MMR × 1000`. "
            "Normalmente no necesitas editar estos valores — los thresholds son fijos por diseño del sistema."
        )
        ranks_dict   = get_rank_config()
        updated_mmrs = {}
        cols_r = st.columns(3)
        for idx,(r_name,r_pts) in enumerate(ranks_dict.items()):
            mmr_val = r_pts / 1000.0
            with cols_r[idx % 3]:
                updated_mmrs[r_name] = st.number_input(
                    f"{r_name}", min_value=0.0, max_value=20.0,
                    value=float(mmr_val), step=0.5, key=f"rank_input_{r_name}",
                    help=f"PR equivalente: {int(r_pts)} PR"
                )
        if st.button("Guardar Configuración de Rangos", type="primary"):
            conn = sqlite3.connect('halo2_cartographer_pro.db'); c = conn.cursor()
            for r_name, mmr_v in updated_mmrs.items():
                c.execute("UPDATE rank_config SET min_points=? WHERE rank_name=?", (mmr_v * 1000.0, r_name))
            conn.commit(); conn.close()
            st.success("¡Umbrales de rangos actualizados!"); st.rerun()

    with tab_reset:
        st.subheader("🧹 Mantenimiento de Base de Datos")
        st.warning(
            "Esta acción borrará **todos** los torneos, partidos, equipos, lineups, "
            "sustituciones, rollbacks y auditoría. "
            "Los jugadores se conservan pero sus PR/MMR se reinician al mínimo de su rango actual."
        )
        if st.button("⚠️ Resetear Todo (Conservar solo Jugadores)", type="primary"):
            conn = sqlite3.connect('halo2_cartographer_pro.db'); c = conn.cursor()
            # Borrar todas las tablas de actividad
            c.execute("DELETE FROM tournaments")
            c.execute("DELETE FROM teams")
            c.execute("DELETE FROM matches")
            c.execute("DELETE FROM match_lineups")
            c.execute("DELETE FROM audit_logs")
            c.execute("DELETE FROM player_substitutions")
            c.execute("DELETE FROM match_rollbacks")
            # Reiniciar autoincrement
            c.execute("""DELETE FROM sqlite_sequence WHERE name IN
                ('tournaments','teams','matches','audit_logs',
                 'player_substitutions','match_rollbacks')""")
            # Resetear PR/MMR de cada modalidad al mínimo del rango actual
            _rst_pr_case = ("CASE {rc} WHEN 'D' THEN 1000 WHEN 'D+' THEN 1500 WHEN 'C' THEN 2000 "
                            "WHEN 'C+' THEN 2500 WHEN 'B' THEN 3000 WHEN 'B+' THEN 3500 "
                            "WHEN 'A' THEN 4000 WHEN 'A+' THEN 4500 WHEN 'S' THEN 5000 "
                            "WHEN 'S+' THEN 5500 WHEN 'S++' THEN 6000 ELSE 6500 END")
            for mode, (mc_r, rc_r, pc_r) in MODE_COLS.items():
                pr_case_r = _rst_pr_case.format(rc=rc_r)
                c.execute(f"""
                    UPDATE players SET
                        {pc_r} = {pr_case_r},
                        {mc_r} = CAST(CAST({pr_case_r} / 500 AS INTEGER) AS REAL) * 0.5
                """)
            pr_case_gen = _rst_pr_case.format(rc="rank_category")
            c.execute(f"""
                UPDATE players SET
                    mmr = CAST(CAST({pr_case_gen} / 500 AS INTEGER) AS REAL) * 0.5,
                    tournament_points = {pr_case_gen}
            """)
            conn.commit(); conn.close()
            st.session_state["toast_msg"] = "¡Reset completo! Torneos, partidos, sustituciones y auditoría borrados. Jugadores y rangos conservados."
            st.rerun()

# ---------------------------------------------------------
# SECCIÓN 7: AUDITORÍA & NOVEDADES
# ---------------------------------------------------------
elif menu == "📋 Auditoría & Novedades":
    st.header("📋 Auditoría & Novedades")
    conn = sqlite3.connect('halo2_cartographer_pro.db')
    tournaments_aud = conn.execute("SELECT id,name,status FROM tournaments ORDER BY id DESC").fetchall()
    if not tournaments_aud:
        st.info("No hay torneos registrados.")
        conn.close()
    else:
        def _t_label_aud(t):
            icon = "🟢" if t[2]=="In Progress" else "🏆" if t[2]=="Completed" else "⚪"
            return f"{icon} #{t[0]} — {t[1]}"
        t_opts_aud = {_t_label_aud(t): t[0] for t in tournaments_aud}
        sel_aud = st.selectbox("Seleccionar Torneo", list(t_opts_aud.keys()), key="aud_torneo_sel")
        t_id_aud = t_opts_aud[sel_aud]

        teams_aud = conn.execute("SELECT id, team_name FROM teams WHERE tournament_id=?", (t_id_aud,)).fetchall()
        team_names_aud = [t[1] for t in teams_aud]
        team_id_map_aud = {t[1]: t[0] for t in teams_aud}

        st.subheader("🔍 Filtros de Auditoría")
        f1, f2, f3 = st.columns(3)
        filt_team   = f1.selectbox("Filtrar por Equipo", ["Todos"] + team_names_aud, key="aud_team")
        filt_player = f2.text_input("Filtrar por Jugador", placeholder="Nombre exacto o parcial...", key="aud_player")
        filt_type   = f3.selectbox("Tipo de Evento",
                        ["Todos", "Inicio de Partida", "Resultados de Partidas",
                         "Sustituciones / Novedades", "Ajustes Admin"], key="aud_type")
        st.divider()

        events_aud = []

        matches_aud_rows = conn.execute("""
            SELECT m.id, m.round_name, m.status, m.started_at, m.registered_by, m.score,
                   m.initial_match_snapshot, m.final_result_snapshot,
                   t1.team_name, t2.team_name, m.team1_id, m.team2_id, m.winner_id
            FROM matches m
            LEFT JOIN teams t1 ON m.team1_id=t1.id
            LEFT JOIN teams t2 ON m.team2_id=t2.id
            WHERE m.tournament_id=?
        """, (t_id_aud,)).fetchall()

        for row in matches_aud_rows:
            (m_id, rn, m_status, started_at, reg_by, m_score,
             init_snap_str, final_snap_str, tn1, tn2, ti1, ti2, win_id) = row

            if init_snap_str:
                try:
                    init_snap = json.loads(init_snap_str)
                    ts = init_snap.get("startedAt", started_at or "")
                    events_aud.append({
                        "type": "Inicio de Partida", "timestamp": ts,
                        "match_id": m_id, "round_name": rn,
                        "team1": tn1 or "—", "team2": tn2 or "—",
                        "team1_id": ti1, "team2_id": ti2,
                        "snapshot": init_snap, "judge": init_snap.get("judgeBy", "—")
                    })
                except Exception:
                    pass

            if final_snap_str:
                try:
                    final_snap = json.loads(final_snap_str)
                    ts = final_snap.get("completedAt", "")
                    winner_name = tn1 if win_id == ti1 else (tn2 if win_id == ti2 else "—")
                    events_aud.append({
                        "type": "Resultados de Partidas", "timestamp": ts,
                        "match_id": m_id, "round_name": rn,
                        "team1": tn1 or "—", "team2": tn2 or "—",
                        "team1_id": ti1, "team2_id": ti2,
                        "winner": winner_name,
                        "score": final_snap.get("score", "—"),
                        "registered_by": final_snap.get("registeredBy", "—"),
                        "snapshot": final_snap
                    })
                except Exception:
                    pass

        subs_rows = conn.execute("""
            SELECT ps.id, ps.match_id, ps.team_id, ps.player_out_id, ps.player_in_id,
                   ps.reason, ps.substituted_at, ps.player_in_snapshot,
                   po.name, pi.name, t.team_name, m.round_name
            FROM player_substitutions ps
            JOIN players po ON ps.player_out_id=po.id
            JOIN players pi ON ps.player_in_id=pi.id
            JOIN teams t ON ps.team_id=t.id
            JOIN matches m ON ps.match_id=m.id
            WHERE m.tournament_id=?
        """, (t_id_aud,)).fetchall()
        for row in subs_rows:
            (_, sub_mid, sub_tid, _, _, reason, sub_at,
             pin_snap_str, pout_name, pin_name, team_name, sub_rn) = row
            try:
                pin_snap = json.loads(pin_snap_str) if pin_snap_str else {}
            except Exception:
                pin_snap = {}
            events_aud.append({
                "type": "Sustituciones / Novedades", "timestamp": sub_at or "",
                "match_id": sub_mid, "round_name": sub_rn,
                "team": team_name, "team_id": sub_tid,
                "player_out": pout_name, "player_in": pin_name,
                "reason": reason, "player_in_snapshot": pin_snap
            })

        rolls_rows = conn.execute("""
            SELECT mr.match_id, mr.admin_user, mr.reason, mr.executed_at, m.round_name
            FROM match_rollbacks mr
            JOIN matches m ON mr.match_id=m.id
            WHERE m.tournament_id=?
        """, (t_id_aud,)).fetchall()
        for row in rolls_rows:
            (roll_mid, admin_user, reason, exec_at, roll_rn) = row
            events_aud.append({
                "type": "Ajustes Admin", "timestamp": exec_at or "",
                "match_id": roll_mid, "round_name": roll_rn,
                "admin": admin_user, "reason": reason
            })

        def _norm_ts(ts):
            return ts.replace("T", " ").replace("Z", "").strip() if ts else ""
        events_aud.sort(key=lambda e: _norm_ts(e.get("timestamp", "")), reverse=True)

        def _ev_team_ok(ev, tf):
            if tf == "Todos": return True
            tid = team_id_map_aud.get(tf)
            if ev["type"] in ("Inicio de Partida", "Resultados de Partidas"):
                return ev.get("team1_id") == tid or ev.get("team2_id") == tid
            if ev["type"] == "Sustituciones / Novedades":
                return ev.get("team_id") == tid
            return True

        def _ev_player_ok(ev, pf):
            if not pf: return True
            pfl = pf.lower()
            if ev["type"] == "Inicio de Partida":
                for tk in ["teamA", "teamB"]:
                    for r in ev.get("snapshot", {}).get("teams", {}).get(tk, {}).get("roster", []):
                        if pfl in r.get("gamertag", "").lower(): return True
                return False
            if ev["type"] == "Resultados de Partidas":
                for pr in ev.get("snapshot", {}).get("playerResults", []):
                    if pfl in pr.get("gamertag", "").lower(): return True
                return False
            if ev["type"] == "Sustituciones / Novedades":
                return pfl in ev.get("player_out","").lower() or pfl in ev.get("player_in","").lower()
            return True

        filtered_aud = [
            e for e in events_aud
            if _ev_team_ok(e, filt_team)
            and _ev_player_ok(e, filt_player)
            and (filt_type == "Todos" or e["type"] == filt_type)
        ]

        st.caption(f"Mostrando **{len(filtered_aud)}** eventos de **{len(events_aud)}** totales")

        if not filtered_aud:
            st.info("No hay eventos que coincidan con los filtros seleccionados.")
        else:
            for ev in filtered_aud:
                ev_type = ev["type"]
                ts_raw  = ev.get("timestamp", "")
                ts_disp = ts_raw[:19].replace("T", " ") if ts_raw else "—"

                if ev_type == "Inicio de Partida":
                    snap = ev.get("snapshot", {})
                    ta   = snap.get("teams", {}).get("teamA", {})
                    tb   = snap.get("teams", {}).get("teamB", {})
                    def _roster_html(team_data):
                        return "".join(
                            f"<div style='margin:2px 0;'><span class='player-tag'>{r['gamertag']}</span> "
                            f"<span style='color:#94a3b8;font-size:0.78rem;'>{r['rankBefore']} · {r['prBefore']} PR · {r['mmrBefore']} MMR</span></div>"
                            for r in team_data.get("roster", [])
                        )
                    st.markdown(f"""
                    <div style='border:1px solid #0284c722;border-left:4px solid #0284c7;border-radius:8px;padding:14px 18px;margin-bottom:10px;background:#0284c708;'>
                        <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;'>
                            <span style='background:#0284c722;color:#0284c7;padding:2px 10px;border-radius:10px;font-size:0.75rem;font-weight:700;'>▶️ INICIO DE PARTIDA</span>
                            <span style='color:#64748b;font-size:0.78rem;'>🕐 {ts_disp} · 🏟️ {ev.get("round_name","—")} · 👤 Juez: {ev.get("judge","—")}</span>
                        </div>
                        <div style='display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:start;'>
                            <div><b style='color:#e2e8f0;'>{ta.get("name","—")}</b> <span style='color:#64748b;font-size:0.78rem;'>MMR Total: {ta.get("totalMMR","—")}</span><div style='margin-top:6px;'>{_roster_html(ta)}</div></div>
                            <div style='text-align:center;color:#0284c7;font-weight:900;padding-top:4px;'>VS</div>
                            <div><b style='color:#e2e8f0;'>{tb.get("name","—")}</b> <span style='color:#64748b;font-size:0.78rem;'>MMR Total: {tb.get("totalMMR","—")}</span><div style='margin-top:6px;'>{_roster_html(tb)}</div></div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                elif ev_type == "Resultados de Partidas":
                    snap    = ev.get("snapshot", {})
                    results = snap.get("playerResults", [])
                    win_rows  = [r for r in results if r.get("result") == "WIN"]
                    loss_rows = [r for r in results if r.get("result") == "LOSS"]
                    def _pr_row(r):
                        delta = r.get("prChange", 0)
                        color = "#22c55e" if delta >= 0 else "#ef4444"
                        sign  = "+" if delta >= 0 else ""
                        rank_chg = "" if r.get("rankBefore") == r.get("rankAfter") else f" → <b>{r.get('rankAfter','')}</b>"
                        bonuses  = ", ".join(r.get("appliedBonuses", []))
                        return (
                            f"<div style='margin:3px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;'>"
                            f"<span class='player-tag'>{r['gamertag']}</span>"
                            f"<span style='color:{color};font-weight:700;font-size:0.85rem;'>{sign}{delta} PR</span>"
                            f"<span style='color:#64748b;font-size:0.75rem;'>{r.get('prBefore','?')} → {r.get('prAfter','?')} PR · {r.get('rankBefore','')}{rank_chg}</span>"
                            f"<span style='color:#475569;font-size:0.72rem;'>{bonuses}</span>"
                            f"</div>"
                        )
                    win_html  = "".join(_pr_row(r) for r in win_rows)
                    loss_html = "".join(_pr_row(r) for r in loss_rows)
                    st.markdown(f"""
                    <div style='border:1px solid #22c55e22;border-left:4px solid #22c55e;border-radius:8px;padding:14px 18px;margin-bottom:10px;background:#22c55e08;'>
                        <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;'>
                            <span style='background:#22c55e22;color:#22c55e;padding:2px 10px;border-radius:10px;font-size:0.75rem;font-weight:700;'>✅ RESULTADO DE PARTIDA</span>
                            <span style='color:#64748b;font-size:0.78rem;'>🕐 {ts_disp} · 🏟️ {ev.get("round_name","—")} · 👤 {ev.get("registered_by","—")}</span>
                        </div>
                        <div style='margin-bottom:8px;'>
                            <span style='font-weight:700;color:#fbbf24;font-size:1.05rem;'>🏆 {ev.get("winner","—")}</span>
                            <span style='color:#94a3b8;margin-left:10px;font-size:0.88rem;'>Marcador: <b>{ev.get("score","—")}</b></span>
                        </div>
                        <div style='display:grid;grid-template-columns:1fr 1fr;gap:10px;'>
                            <div><div style='color:#22c55e;font-size:0.78rem;font-weight:700;margin-bottom:4px;'>GANADORES</div>{win_html or "—"}</div>
                            <div><div style='color:#ef4444;font-size:0.78rem;font-weight:700;margin-bottom:4px;'>PERDEDORES</div>{loss_html or "—"}</div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                elif ev_type == "Sustituciones / Novedades":
                    pin_snap  = ev.get("player_in_snapshot", {})
                    pin_stats = f"{pin_snap.get('rank','—')} · {pin_snap.get('pr','—')} PR · {pin_snap.get('mmr','—')} MMR" if pin_snap else "—"
                    st.markdown(f"""
                    <div style='border:1px solid #f59e0b22;border-left:4px solid #f59e0b;border-radius:8px;padding:14px 18px;margin-bottom:10px;background:#f59e0b08;'>
                        <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'>
                            <span style='background:#f59e0b22;color:#f59e0b;padding:2px 10px;border-radius:10px;font-size:0.75rem;font-weight:700;'>🔄 SUSTITUCIÓN / NOVEDAD</span>
                            <span style='color:#64748b;font-size:0.78rem;'>🕐 {ts_disp} · 🏟️ {ev.get("round_name","—")}</span>
                        </div>
                        <div style='color:#e2e8f0;'>
                            <b>{ev.get("player_in","—")}</b> ingresó en reemplazo de <b>{ev.get("player_out","—")}</b> en el equipo <b>{ev.get("team","—")}</b>
                        </div>
                        <div style='color:#94a3b8;font-size:0.82rem;margin-top:4px;'>Motivo: {ev.get("reason","—")} · Stats entrada: {pin_stats}</div>
                    </div>
                    """, unsafe_allow_html=True)

                elif ev_type == "Ajustes Admin":
                    st.markdown(f"""
                    <div style='border:1px solid #a855f722;border-left:4px solid #a855f7;border-radius:8px;padding:14px 18px;margin-bottom:10px;background:#a855f708;'>
                        <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'>
                            <span style='background:#a855f722;color:#a855f7;padding:2px 10px;border-radius:10px;font-size:0.75rem;font-weight:700;'>⚙️ AJUSTE ADMIN / ROLLBACK</span>
                            <span style='color:#64748b;font-size:0.78rem;'>🕐 {ts_disp} · 🏟️ {ev.get("round_name","—")}</span>
                        </div>
                        <div style='color:#e2e8f0;'>Admin: <b>{ev.get("admin","—")}</b> — {ev.get("reason","—")}</div>
                    </div>
                    """, unsafe_allow_html=True)

        st.divider()
        with st.expander("⚙️ Registrar Rollback / Ajuste Admin"):
            matches_for_roll = conn.execute("""
                SELECT m.id, m.round_name, t1.team_name, t2.team_name
                FROM matches m
                LEFT JOIN teams t1 ON m.team1_id=t1.id
                LEFT JOIN teams t2 ON m.team2_id=t2.id
                WHERE m.tournament_id=? AND m.status IN ('COMPLETED','IN_PROGRESS')
            """, (t_id_aud,)).fetchall()
            if matches_for_roll:
                roll_opts = {f"Partida #{r[0]} — {r[1]} ({r[2] or '?'} vs {r[3] or '?'})": r[0] for r in matches_for_roll}
                roll_sel    = st.selectbox("Partida afectada", list(roll_opts.keys()), key="roll_match_sel")
                roll_mid    = roll_opts[roll_sel]
                roll_admin  = st.text_input("Admin responsable", key="roll_admin")
                roll_reason = st.text_area("Motivo del ajuste / rollback", key="roll_reason", height=80)
                if st.button("Registrar Rollback", key="roll_submit", type="primary"):
                    if roll_admin and roll_reason:
                        prev_snap = conn.execute("SELECT final_result_snapshot FROM matches WHERE id=?", (roll_mid,)).fetchone()
                        prev_snap_str = prev_snap[0] if prev_snap and prev_snap[0] else "{}"
                        conn.execute(
                            "INSERT INTO match_rollbacks (match_id,tournament_id,admin_user,reason,executed_at,previous_snapshot) VALUES (?,?,?,?,?,?)",
                            (roll_mid, t_id_aud, roll_admin, roll_reason,
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'), prev_snap_str)
                        )
                        conn.commit()
                        log_action(roll_admin, "Rollback", f"Partida #{roll_mid}: {roll_reason}")
                        st.session_state["toast_msg"] = "Rollback registrado en el histórico de auditoría."
                        st.rerun()
                    else:
                        st.error("Admin y Motivo son obligatorios.")
            else:
                st.info("No hay partidas completadas o en progreso para registrar rollback.")
        conn.close()
