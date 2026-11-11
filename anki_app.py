import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import concurrent.futures
import os
import unicodedata
import html
from collections.abc import Mapping
import streamlit.components.v1 as components

# --- 文字列比較用のNFC正規化ヘルパー関数 ---
def normalize_nfc(text):
    """比較・検索・グループ化用に文字列をNFC正規化する（ファイル操作には使用しない）"""
    if text is None:
        return ""
    return unicodedata.normalize('NFC', str(text))

# --- ページ設定（画面をより大きく使うため layout="wide" に設定） ---
st.set_page_config(page_title="暗記リストアプリ", page_icon="📖", layout="wide")

# --- 表示サイズ・強調ラベル列のデフォルト設定 ---
# 基本フォントサイズ（px）
DEFAULT_BASE_FONT_SIZE = 22
# 強調表示するラベル列（複数指定可能。初期値として「語呂合わせ」を設定）
DEFAULT_HIGHLIGHT_COLS = ["語呂合わせ"]
# 強調表示するラベル列の文字サイズ倍率（他のラベル列に対する倍率）
DEFAULT_HIGHLIGHT_SCALE = 1.5
# 強調表示するラベル列の文字色（カラーコードまたは色名）
DEFAULT_HIGHLIGHT_COLOR = "#e60000"  # 赤

# 画面全体のフォントサイズ・余白を拡大するカスタムCSS
st.markdown(
    """
    <style>
        .block-container {
            max-width: 1100px !important;
            padding-top: 2rem !important;
            padding-bottom: 3rem !important;
        }
        /* ボタン内のテキストも大きく見やすくする */
        .stButton > button {
            font-size: 1.2rem !important;
            padding: 0.7rem 1.2rem !important;
            height: auto !important;
        }
    </style>
    """,
    unsafe_allow_html=True
)

# --- Googleスプレッドシート連携の設定 ---
CREDENTIALS_FILE = "secret_key.json"

# 【重要】ここの単元名と文字列を、ご自身のスプレッドシートのキーに書き換えてください
SPREADSHEET_KEYS = {
    "特許": "1GOA12mzr6yWzm1Cdu-TMmnQdZ_Y8goV5I5MT9IcPKLw",
    "実用新案":"1vD98TVxux7IEGbuc1ZV1IUyqjwbGVrhu4HGU9_RNmvM",
    "意匠":"1wdZD-grY1wCwNi1ylaIsUfNp72KQ6fet5A_WrZnlliE",
    "商標":"1qW1OwViuw5LAP2rIoPvC65R3Bh0dMZvhegbujjzDXCE",
    "条約・協定":"1ikoeUqbk8e13ASrku3hVpGUxmGsaPi-F1TANbJVhre0",
    "不正競争・著作権":"1L3Wu4POBTHuEITpHzpSot2Np5qCgOJqvOM8EGEiU5Bs"
}


@st.cache_resource
def _authorize_cached_client(creds_json_str):
    """認証に成功したクライアントのみをキャッシュする内部関数（エラーをキャッシュさせない）"""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(creds_json_str)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def get_gspread_client():
    """
    Google APIクライアントを取得する。
    エラー発生時はキャッシュせず、原因を確実に特定できる詳細なデバッグ診断情報を返す。
    """
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    debug_notes = []

    try:
        # 1. st.secrets の読み取りとキー一覧の診断
        secret_keys = []
        secrets_accessible = False
        try:
            secret_keys = list(st.secrets.keys())
            secrets_accessible = True
            debug_notes.append(f"st.secrets 内で検出されたキー一覧: {secret_keys if secret_keys else 'なし（空）'}")
        except Exception as se:
            debug_notes.append(f"st.secrets へのアクセス時に例外が発生: {type(se).__name__}: {se}")

        # NFC正規化を用いて 'gcp_service_account' キーを検索（前後の空白や全角スペース混入等も考慮）
        target_key_nfc = normalize_nfc("gcp_service_account")
        matched_secret_key = None
        if secrets_accessible:
            for k in secret_keys:
                if normalize_nfc(k).strip() == target_key_nfc:
                    matched_secret_key = k
                    break

        if matched_secret_key is not None:
            raw_val = st.secrets[matched_secret_key]
            debug_notes.append(f"対象キー '{matched_secret_key}' のデータ型: {type(raw_val).__name__}")

            creds_dict = None
            # ケースA: TOMLセクション等として既に辞書型（Mapping）で格納されている場合
            if isinstance(raw_val, Mapping):
                creds_dict = dict(raw_val)
            # ケースB: 文字列（''' { ... } '''）として格納されている場合
            else:
                raw_str = str(raw_val)
                debug_notes.append(f"文字列長: {len(raw_str)} 文字")
                # ノーブレークスペース(\u00a0)やBOM(\ufeff)が混入している場合は除去・置換
                cleaned_str = raw_str.replace('\u00a0', ' ').replace('\ufeff', '').strip()

                try:
                    creds_dict = json.loads(cleaned_str)
                except json.JSONDecodeError as je:
                    # 秘密鍵内の実際の改行コードが原因でパースエラーになる場合の自動補正試行
                    try:
                        creds_dict = json.loads(cleaned_str, strict=False)
                    except Exception as je2:
                        err_pos = getattr(je, 'pos', 0)
                        snippet = cleaned_str[max(0, err_pos - 25):min(len(cleaned_str), err_pos + 25)]
                        return None, (
                            f"SecretsのJSON形式の解析に失敗しました。\n"
                            f"- **エラー箇所**: {je.lineno}行目 {je.colno}文字目付近 (`{je.msg}`)\n"
                            f"- **該当箇所の前後**: `{ repr(snippet) }`\n"
                            f"- **診断ログ**: {' / '.join(debug_notes)}"
                        )

            # 必須キーの確認と秘密鍵内の "\\n" エスケープ補正
            if isinstance(creds_dict, dict):
                if "private_key" in creds_dict and isinstance(creds_dict["private_key"], str):
                    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

                normalized_json_str = json.dumps(creds_dict, ensure_ascii=False, sort_keys=True)
                client = _authorize_cached_client(normalized_json_str)
                return client, None

        # ケースC: トップレベルに直接 type="service_account" 等が書かれている場合
        elif secrets_accessible and any(normalize_nfc(k).strip() == normalize_nfc("private_key") for k in secret_keys):
            debug_notes.append("トップレベルに直接サービスアカウント情報が検出されました。")
            creds_dict = {str(k).strip(): st.secrets[k] for k in secret_keys}
            if "private_key" in creds_dict and isinstance(creds_dict["private_key"], str):
                creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            normalized_json_str = json.dumps(creds_dict, ensure_ascii=False, sort_keys=True)
            client = _authorize_cached_client(normalized_json_str)
            return client, None

        # 2. ローカルファイル (CREDENTIALS_FILE) の確認
        # ファイル操作には正規化前の元のパス文字列(CREDENTIALS_FILE)をそのまま使用
        if os.path.exists(CREDENTIALS_FILE):
            creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
            return gspread.authorize(creds), None
        else:
            debug_notes.append(f"ローカルファイル '{CREDENTIALS_FILE}' の存在: False (カレントディレクトリ: {os.getcwd()})")

        # 3. どちらも見つからない場合の詳細デバッグメッセージ生成
        return None, (
            "【エラー】Streamlit Cloudの「Secrets」から `gcp_service_account` を読み取れませんでした。\n\n"
            "**🔍 【原因特定の診断結果】**\n"
            + "\n".join(f"- {note}" for note in debug_notes) + "\n\n"
            "**💡 【確認・対処手順】**\n"
            "1. **特殊空白文字の除去**: Secretsに貼り付けたテキスト内に特殊空白（ノーブレークスペース等）が含まれていると、TOML全体の読み込みが失敗してキー一覧が「なし（空）」になります。クリーンな半角スペースのテキストを貼り直して「Save」を押してください。\n"
            "2. **設定先アプリの確認**: 「正誤クイズアプリ」と「暗記リストアプリ」でURLが異なる場合、現在開いているアプリ側の `Manage app ＞ Settings ＞ Secrets` に鍵が保存されているか確認してください。\n"
            "3. **キャッシュのクリア**: Secrets保存直後の場合は、下の「🔄 認証キャッシュをクリアして再接続」ボタンを押してください。"
        )

    except Exception as e:
        return None, (
            f"認証処理中に予期せぬエラーが発生しました: `{type(e).__name__}: {str(e)}`\n"
            f"- **診断ログ**: {' / '.join(debug_notes)}"
        )

# --- セッション状態の初期化 ---
if 'stage' not in st.session_state:
    st.session_state.stage = 'home'
if 'resume_info' not in st.session_state:
    st.session_state.resume_info = None
if 'all_df' not in st.session_state:
    st.session_state.all_df = pd.DataFrame()
if 'questions' not in st.session_state:
    st.session_state.questions = []
if 'current_idx' not in st.session_state:
    st.session_state.current_idx = 0
if 'pending_updates' not in st.session_state:
    st.session_state.pending_updates = {}
if 'sheet_col_meta' not in st.session_state:
    st.session_state.sheet_col_meta = {}
if 'load_debug_info' not in st.session_state:
    st.session_state.load_debug_info = {}

# 表示スタイル・強調設定用のセッション状態
if 'base_font_size' not in st.session_state:
    st.session_state.base_font_size = DEFAULT_BASE_FONT_SIZE
if 'highlight_cols' not in st.session_state:
    st.session_state.highlight_cols = list(DEFAULT_HIGHLIGHT_COLS)
if 'highlight_scale' not in st.session_state:
    st.session_state.highlight_scale = float(DEFAULT_HIGHLIGHT_SCALE)
if 'highlight_color' not in st.session_state:
    st.session_state.highlight_color = DEFAULT_HIGHLIGHT_COLOR

# ==========================================
# 列位置特定・並列処理用ヘルパー関数群
# ==========================================
def _detect_sheet_columns(headers):
    """
    ヘッダーリストから「要注意」列と「出題順序」列の1始まりの列番号、
    および画面表示対象となるラベル列名のリストを特定する。
    """
    norm_headers = [normalize_nfc(h).strip() for h in headers]
    
    caution_target = normalize_nfc("要注意")
    wrong_target = normalize_nfc("間違えた回数")
    order_target = normalize_nfc("出題順序")
    cat_target1 = normalize_nfc("分野")
    cat_target2 = normalize_nfc("子フォルダ名")
    
    # 「要注意」列の特定（見つからない場合は既存のデータ列を上書きしないよう右端の空き列または5列目を使用）
    caution_col_idx = None
    for idx, nh in enumerate(norm_headers):
        if nh == caution_target or nh == wrong_target:
            caution_col_idx = idx + 1
            break
    if caution_col_idx is None:
        non_empty_indices = [idx + 1 for idx, nh in enumerate(norm_headers) if nh != ""]
        max_used_col = max(non_empty_indices) if non_empty_indices else 4
        caution_col_idx = max(5, max_used_col + 1)

    # 「出題順序」列の特定（見つからない場合は「要注意」列の右隣または空き列とする）
    order_col_idx = None
    for idx, nh in enumerate(norm_headers):
        if nh == order_target:
            order_col_idx = idx + 1
            break
    if order_col_idx is None:
        non_empty_indices = [idx + 1 for idx, nh in enumerate(norm_headers) if nh != ""]
        max_used_col = max(non_empty_indices) if non_empty_indices else 4
        order_col_idx = max(caution_col_idx + 1, max_used_col + 1)
        if order_col_idx == caution_col_idx:
            order_col_idx += 1

    # 表示用ラベル列（「分野」「要注意」「出題順序」および空白ヘッダーを除外）
    display_cols = []
    for idx, orig_h in enumerate(headers):
        col_num = idx + 1
        nh = norm_headers[idx]
        if col_num in (caution_col_idx, order_col_idx):
            continue
        if nh in ("", cat_target1, cat_target2, caution_target, wrong_target, order_target):
            continue
        display_cols.append(str(orig_h).strip())

    return caution_col_idx, order_col_idx, display_cols

def _check_single_sheet_resume(subj, key, client):
    try:
        ws = client.open_by_key(key).sheet1
        data = ws.get_all_values()
        if len(data) <= 1:
            return subj, 0
        headers = [str(h).strip() for h in data[0]]
        _, order_col_idx, _ = _detect_sheet_columns(headers)
        
        count = 0
        for row in data[1:]:
            if len(row) >= order_col_idx:
                val = str(row[order_col_idx - 1]).strip()
                if val.isdigit() and int(val) > 0:
                    count += 1
        return subj, count
    except Exception as e:
        return subj, e

def _load_single_sheet_data(subj, key, client):
    try:
        sh = client.open_by_key(key)
        ws = sh.sheet1
        sheet_title = ws.title
        data = ws.get_all_values()
        total_rows = len(data)
        raw_headers = [str(h).strip() for h in data[0]] if total_rows > 0 else []

        if total_rows > 1:
            caution_col_idx, order_col_idx, display_cols = _detect_sheet_columns(raw_headers)
            
            # 必要な列数分だけ各行の長さを揃える
            max_needed_cols = max(len(raw_headers), caution_col_idx, order_col_idx)
            padded_headers = list(raw_headers)
            while len(padded_headers) < max_needed_cols:
                padded_headers.append("")
                
            col_names = list(padded_headers)
            col_names[caution_col_idx - 1] = "要注意"
            col_names[order_col_idx - 1] = "出題順序"
            
            # 分野列の正規化判定と統一（最初に見つかった1列のみを「分野」とする）
            found_cat = False
            for idx, h in enumerate(col_names):
                nh = normalize_nfc(h)
                if nh == normalize_nfc("子フォルダ名") or nh == normalize_nfc("分野"):
                    if not found_cat:
                        col_names[idx] = "分野"
                        found_cat = True
            if not found_cat and len(col_names) > 0 and col_names[0] not in ("要注意", "出題順序"):
                if col_names[0] in display_cols:
                    display_cols.remove(col_names[0])
                col_names[0] = "分野"

            # 同名の列名が複数ある場合にDataFrame結合エラーを防ぐためユニーク化
            unique_col_names = []
            seen_counts = {}
            display_cols_updated = []
            for idx, c in enumerate(col_names):
                if c == "":
                    unique_col_names.append("")
                    continue
                c_nfc = normalize_nfc(c)
                if c_nfc in seen_counts:
                    seen_counts[c_nfc] += 1
                    new_c = f"{c}_{seen_counts[c_nfc]}"
                else:
                    seen_counts[c_nfc] = 1
                    new_c = c
                unique_col_names.append(new_c)
                if (idx + 1) not in (caution_col_idx, order_col_idx) and new_c != "分野":
                    display_cols_updated.append(new_c)
            display_cols = display_cols_updated
                    
            padded_rows = [
                r + [""] * (max_needed_cols - len(r)) if len(r) < max_needed_cols else r[:max_needed_cols]
                for r in data[1:]
            ]
            df = pd.DataFrame(padded_rows, columns=unique_col_names)
            
            # 空欄列名があれば除去
            df = df.loc[:, [c != "" for c in df.columns]]
                
            # 元の行番号（スプレッドシート上の行番号：2行目始まり）を保持
            df['row_num'] = df.index + 2
            
            # 表示対象列または分野に値がある有効行のみ抽出
            check_cols = [c for c in display_cols if c in df.columns]
            if check_cols:
                valid_mask = df[check_cols].astype(str).apply(lambda s: s.str.strip() != '').any(axis=1)
                df = df[valid_mask].copy()
            elif '分野' in df.columns:
                df = df[df['分野'].astype(str).str.strip() != ''].copy()
                
            if '要注意' not in df.columns:
                df['要注意'] = 0
            if '出題順序' not in df.columns:
                df['出題順序'] = 0
                
            df['要注意'] = pd.to_numeric(df['要注意'], errors='coerce').fillna(0).astype(int)
            df['出題順序'] = pd.to_numeric(df['出題順序'], errors='coerce').fillna(0).astype(int)
            df['科目'] = subj
            df['_display_cols'] = [display_cols for _ in range(len(df))]
            
            meta = {
                'caution_col': caution_col_idx,
                'order_col': order_col_idx,
                'display_cols': display_cols
            }
            debug_info = {
                'status': 'success' if not df.empty else 'empty_after_filter',
                'sheet_title': sheet_title,
                'total_rows': total_rows,
                'valid_rows': len(df),
                'raw_headers': raw_headers,
                'display_cols': display_cols,
                'has_category_col': '分野' in df.columns,
                'non_empty_categories': int((df['分野'].astype(str).str.strip() != '').sum()) if '分野' in df.columns and not df.empty else 0
            }
            return subj, (df, meta, debug_info)

        debug_info = {
            'status': 'no_data_rows',
            'sheet_title': sheet_title,
            'total_rows': total_rows,
            'valid_rows': 0,
            'raw_headers': raw_headers,
            'display_cols': []
        }
        return subj, (pd.DataFrame(), {'caution_col': 5, 'order_col': 6, 'display_cols': []}, debug_info)
    except Exception as e:
        debug_info = {
            'status': 'error',
            'error_type': type(e).__name__,
            'error_msg': str(e),
            'key': key
        }
        return subj, (e, debug_info)

def _save_single_sheet(subj, key, updates, col_meta, client):
    try:
        ws = client.open_by_key(key).sheet1
        caution_col = col_meta.get('caution_col', 5)
        order_col = col_meta.get('order_col', 6)
        
        min_col = min(caution_col, order_col)
        max_col = max(caution_col, order_col)
        max_row = max(updates.keys())
        
        cells = ws.range(2, min_col, max_row, max_col)
        for cell in cells:
            r = cell.row
            if r in updates:
                if cell.col == caution_col and 'caution' in updates[r]:
                    cell.value = updates[r]['caution']
                elif cell.col == order_col and 'order' in updates[r]:
                    cell.value = updates[r]['order']
        ws.update_cells(cells)
        return subj, True
    except Exception as e:
        return subj, e

def _reset_order_col(subj, key, orders, col_meta, client):
    try:
        ws = client.open_by_key(key).sheet1
        data = ws.get_all_values()
        num_rows = len(data)
        if num_rows > 1:
            if col_meta and 'order_col' in col_meta:
                order_col = col_meta['order_col']
            else:
                headers = [str(h).strip() for h in data[0]]
                _, order_col, _ = _detect_sheet_columns(headers)
                
            f_cells = ws.range(2, order_col, num_rows, order_col)
            for cell in f_cells:
                r = cell.row
                cell.value = orders.get(r, "")
            ws.update_cells(f_cells)
        return True
    except Exception:
        return False

# ==========================================
# メイン処理関数（マルチスレッド並列化）
# ==========================================
def check_resume_state():
    client, err = get_gspread_client()
    if err:
        st.error(f"⚠️ 【システムエラー】Google APIとの接続に失敗しました。\n\n{err}")
        if st.button("🔄 認証キャッシュをクリアして再接続"):
            st.cache_resource.clear()
            st.session_state.resume_info = None
            st.rerun()
        return {'count': 0, 'subjects': []}
        
    total_resume_count = 0
    resume_subjects = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_check_single_sheet_resume, subj, key, client): subj for subj, key in SPREADSHEET_KEYS.items()}
        for future in concurrent.futures.as_completed(futures):
            subj, result = future.result()
            if isinstance(result, int):
                if result > 0:
                    total_resume_count += result
                    resume_subjects.append(subj)
            else:
                st.warning(f"⚠️ 「{subj}」の確認中にエラーが出ました: {result}")
            
    return {'count': total_resume_count, 'subjects': resume_subjects}

def load_data(subjects_to_load):
    client, err = get_gspread_client()
    if err:
        st.error(f"⚠️ 接続エラー:\n\n{err}")
        for subj in subjects_to_load:
            st.session_state.load_debug_info[subj] = {
                'status': 'auth_error',
                'error_msg': str(err)
            }
        return
        
    all_data = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_load_single_sheet_data, subj, SPREADSHEET_KEYS[subj], client): subj for subj in subjects_to_load}
        for future in concurrent.futures.as_completed(futures):
            subj, result = future.result()
            if isinstance(result, tuple) and len(result) == 3:
                df, meta, debug_info = result
                st.session_state.sheet_col_meta[subj] = meta
                st.session_state.load_debug_info[subj] = debug_info
                if not df.empty:
                    all_data.append(df)
            elif isinstance(result, tuple) and len(result) == 2:
                err_obj, debug_info = result
                st.session_state.load_debug_info[subj] = debug_info
                st.error(f"「{subj}」の読み込みに失敗: {err_obj}")
            else:
                st.session_state.load_debug_info[subj] = {'status': 'unknown_error', 'error_msg': str(result)}
                st.error(f"「{subj}」の読み込みに失敗: {result}")
            
    if all_data:
        st.session_state.all_df = pd.concat(all_data, ignore_index=True)
    else:
        st.session_state.all_df = pd.DataFrame()

def save_learning_records():
    if not st.session_state.pending_updates:
        return
        
    client, err = get_gspread_client()
    if err:
        st.error(f"保存エラー:\n\n{err}")
        return
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(
                _save_single_sheet,
                subj,
                SPREADSHEET_KEYS[subj],
                updates,
                st.session_state.sheet_col_meta.get(subj, {'caution_col': 5, 'order_col': 6}),
                client
            ): subj
            for subj, updates in st.session_state.pending_updates.items() if updates
        }
        for future in concurrent.futures.as_completed(futures):
            subj, result = future.result()
            if not isinstance(result, bool):
                st.error(f"保存中にエラー ({subj}): {result}")

    for subj, updates in st.session_state.pending_updates.items():
        for r, vals in updates.items():
            mask = (
                (st.session_state.all_df['科目'].apply(normalize_nfc) == normalize_nfc(subj)) &
                (st.session_state.all_df['row_num'] == r)
            )
            if 'caution' in vals:
                st.session_state.all_df.loc[mask, '要注意'] = 0 if vals['caution'] == "" else int(vals['caution'])
            if 'order' in vals:
                st.session_state.all_df.loc[mask, '出題順序'] = 0 if vals['order'] == "" else int(vals['order'])
            
    st.session_state.pending_updates = {}
    st.session_state.resume_info = None

def _get_available_label_columns():
    """読み込まれている全シートおよび現在の問題リストから選択可能なラベル列一覧を取得する"""
    cols = []
    seen_nfc = set()

    def _add_col(c_name):
        c_str = str(c_name).strip()
        if not c_str:
            return
        c_nfc = normalize_nfc(c_str)
        if c_nfc not in seen_nfc:
            seen_nfc.add(c_nfc)
            cols.append(c_str)

    # デフォルト指定のラベル列（語呂合わせ等）を常に選択肢に含める
    for dc in DEFAULT_HIGHLIGHT_COLS:
        _add_col(dc)
    for hc in st.session_state.highlight_cols:
        _add_col(hc)

    # 各単元のメタ情報から表示対象ラベル列を収集
    for meta in st.session_state.sheet_col_meta.values():
        for c in meta.get('display_cols', []):
            _add_col(c)

    # 現在進行中のquestionsからも収集（再開時などのため）
    for q in st.session_state.questions:
        for c in q.get('_display_cols', []):
            _add_col(c)

    return cols

def _render_style_settings_ui(key_prefix="setup"):
    """文字サイズ・特定ラベル列の強調表示設定を行うUIコンポーネント"""
    available_cols = _get_available_label_columns()
    available_nfc_map = {normalize_nfc(c): c for c in available_cols}

    # セッション内の選択値をNFC正規化で照合して初期選択リストを作成
    default_selected = []
    for hc in st.session_state.highlight_cols:
        hc_nfc = normalize_nfc(hc)
        if hc_nfc in available_nfc_map:
            matched_c = available_nfc_map[hc_nfc]
            if matched_c not in default_selected:
                default_selected.append(matched_c)

    c1, c2 = st.columns(2)
    with c1:
        base_size = st.slider(
            "全体の基本文字サイズ (px)",
            min_value=16,
            max_value=40,
            value=int(st.session_state.base_font_size),
            step=1,
            key=f"{key_prefix}_base_font_size"
        )
        st.session_state.base_font_size = base_size

        selected_hl_cols = st.multiselect(
            "強調表示するラベル列（複数選択可）",
            options=available_cols,
            default=default_selected,
            key=f"{key_prefix}_highlight_cols"
        )
        st.session_state.highlight_cols = selected_hl_cols

    with c2:
        hl_scale = st.number_input(
            "強調ラベル列の文字サイズ倍率（例: 1.5倍）",
            min_value=1.0,
            max_value=4.0,
            value=float(st.session_state.highlight_scale),
            step=0.1,
            format="%.1f",
            key=f"{key_prefix}_highlight_scale"
        )
        st.session_state.highlight_scale = float(hl_scale)

        hl_color = st.color_picker(
            "強調ラベル列の文字色（デフォルト：赤）",
            value=st.session_state.highlight_color,
            key=f"{key_prefix}_highlight_color"
        )
        st.session_state.highlight_color = hl_color

def _render_debug_message(subj):
    """「データがありません。」となった具体的な原因を画面上に診断表示するデバッグ関数"""
    info = st.session_state.load_debug_info.get(subj)
    if not info:
        st.warning(
            f"🔍 **【原因診断：{subj}】**\n\n"
            f"この単元のデータがまだメモリに読み込まれていません（設定画面を開いたままコードの `SPREADSHEET_KEYS` を変更した場合などに発生します）。\n\n"
            f"👉 画面上部の **「🔄 スプレッドシートのデータを再読み込み」** ボタンを押すか、一度トップ画面に戻り再度この画面へ進んでください。"
        )
        return

    status = info.get('status')
    if status == 'error':
        err_type = info.get('error_type', '')
        err_msg = info.get('error_msg', '')
        key_val = info.get('key', '')
        st.error(
            f"🔍 **【原因診断：{subj}】Googleスプレッドシートの読み込み時にエラーが発生しました**\n\n"
            f"- **エラー種類**: `{err_type}`\n"
            f"- **エラー詳細**: `{err_msg}`\n"
            f"- **指定されたキー**: `{key_val}`\n\n"
            f"**💡 対処方法**:\n"
            f"1. `PermissionError` や `APIError(403)` 等が出る場合：対象のスプレッドシートの右上「共有」ボタンから、**サービスアカウントのメールアドレス（`google-drive-api@gen-lang-client-0840508964.iam.gserviceaccount.com`）を「編集者」として追加**してください。\n"
            f"2. `SpreadsheetNotFound` や `404` 等が出る場合：`SPREADSHEET_KEYS` に設定したシートのキー（ID文字列）に誤りがないか確認してください。"
        )
    elif status == 'no_data_rows':
        st.warning(
            f"🔍 **【原因診断：{subj}】スプレッドシートの2行目以降にデータが見つかりません**\n\n"
            f"- **読み込んだシート名（一番左のシート）**: `{info.get('sheet_title')}`\n"
            f"- **取得された総行数**: `{info.get('total_rows')} 行`\n"
            f"- **1行目のヘッダー**: `{info.get('raw_headers')}`\n\n"
            f"**💡 対処方法**: プログラムはスプレッドシートの**一番左にあるシート**を読み込みます。データが2番目以降のシートに入っていないか、2行目以降にデータが入力されているかを確認してください。"
        )
    elif status == 'empty_after_filter':
        st.warning(
            f"🔍 **【原因診断：{subj}】シートは読み込めましたが、表示対象となる有効な行が0件でした**\n\n"
            f"- **読み込んだシート名**: `{info.get('sheet_title')}` （総行数: `{info.get('total_rows')} 行`）\n"
            f"- **1行目のヘッダー**: `{info.get('raw_headers')}`\n"
            f"- **認識された表示用ラベル列**: `{info.get('display_cols')}`\n\n"
            f"**💡 対処方法**: 1行目（ヘッダー行）が空欄になっていないか確認してください。1行目にタイトル行や空行があり、2行目以降にラベル名（分野など）がある場合は、1行目がラベル名になるよう上部の不要な行を削除してください。"
        )
    elif status == 'auth_error':
        st.error(f"🔍 **【原因診断：{subj}】認証エラー**:\n\n{info.get('error_msg')}")
    else:
        st.info(
            f"🔍 **【原因診断：{subj}】詳細ステータス**: `{info}`"
        )

# --- メインUI ---
st.title("📖 暗記リスト学習アプリ")

if st.session_state.stage == 'home':
    if st.session_state.resume_info is None:
        with st.spinner("過去の学習記録を高速スキャンしています..."):
            st.session_state.resume_info = check_resume_state()
            
    resume_info = st.session_state.resume_info
    
    if resume_info['count'] > 0:
        st.success(f"💡 前回中断したセッションの続き（残り {resume_info['count']}件）があります。")
        if st.button("▶️ 前回の続きから再開する", type="primary", use_container_width=True):
            with st.spinner("続きのデータを並列で高速読み込みしています..."):
                load_data(resume_info['subjects'])
                df = st.session_state.all_df
                resume_df = df[df['出題順序'] > 0].sort_values(by='出題順序')
                
                st.session_state.questions = resume_df.to_dict('records')
                st.session_state.current_idx = 0
                st.session_state.pending_updates = {}
                st.session_state.stage = 'quiz'
                st.rerun()
        st.markdown("---")
        
    st.subheader("新規暗記リストを作成する")
    st.caption("※新しい暗記リストを作成すると、前回の続きは破棄されます。")
    if st.button("🆕 新規暗記リストの設定画面へ進む"):
        with st.spinner("全単元のデータを並列で高速読み込みしています..."):
            load_data(list(SPREADSHEET_KEYS.keys()))
            st.session_state.stage = 'setup'
            st.rerun()

elif st.session_state.stage == 'setup':
    st.subheader("新規暗記リストの表示範囲設定")
    st.write("各単元（スプレッドシート）から表示したい分野を選んでください。（全分野を選ぶ場合は、各単元の「全分野を表示する」にチェックを入れます）")
    
    if st.button("🔄 スプレッドシートのデータを再読み込み"):
        with st.spinner("認証キャッシュをクリアし、全単元のデータを再読み込みしています..."):
            st.cache_resource.clear()
            load_data(list(SPREADSHEET_KEYS.keys()))
            st.rerun()

    all_df = st.session_state.all_df
    selected_dfs = []
    
    for subj in SPREADSHEET_KEYS.keys():
        with st.expander(f"📚 {subj}", expanded=False):
            if all_df.empty:
                st.write("データがありません。")
                _render_debug_message(subj)
                continue
            
            subj_norm = normalize_nfc(subj)
            subj_df = all_df[all_df['科目'].apply(normalize_nfc) == subj_norm]
            if subj_df.empty:
                st.write("データがありません。")
                _render_debug_message(subj)
                continue
                
            # 分野の重複排除（NFC正規化ベースで比較しつつ元の表記を保持）
            categories = []
            seen_cats_nfc = set()
            for cat in subj_df['分野'].dropna():
                cat_str = str(cat).strip()
                if not cat_str:
                    continue
                cat_nfc = normalize_nfc(cat_str)
                if cat_nfc not in seen_cats_nfc:
                    seen_cats_nfc.add(cat_nfc)
                    categories.append(cat_str)
            
            if not categories:
                st.warning(
                    f"⚠️ データは `{len(subj_df)}件` 読み込まれましたが、「分野」列（A列）がすべて空欄のため分野を選択できません。"
                    f"（認識された1行目のヘッダー: `{st.session_state.load_debug_info.get(subj, {}).get('raw_headers', [])}`）"
                )

            use_all = st.checkbox(f"「{subj}」の全分野を表示する", key=f"chk_{subj}")
            if use_all:
                selected_dfs.append(subj_df)
            else:
                sel_cats = st.multiselect(
                    "分野を選択（未選択の場合はこの単元からは表示されません）", 
                    options=categories, 
                    key=f"sel_{subj}"
                )
                if sel_cats:
                    sel_cats_nfc = {normalize_nfc(c) for c in sel_cats}
                    matched_df = subj_df[subj_df['分野'].apply(lambda x: normalize_nfc(str(x).strip()) in sel_cats_nfc)]
                    selected_dfs.append(matched_df)
    
    st.markdown("---")
    st.subheader("🎨 文字サイズ・強調表示の設定")
    st.caption("特定のラベル列（デフォルト：「語呂合わせ」）の記載事項を、指定した倍率・文字色で強調表示できます。")
    _render_style_settings_ui(key_prefix="setup")

    st.markdown("---")
    mode_seq = "先頭から順に表示"
    mode_rand = "ランダムに表示"
    mode_caution = "要注意のものから表示"
    modes = [mode_seq, mode_rand, mode_caution]
    selected_mode = st.radio("暗記リスト表示方法を選択", modes)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("暗記リストスタート！", type="primary", use_container_width=True):
            if not selected_dfs:
                st.warning("表示する分野が1つも選択されていません。")
            else:
                target_df = pd.concat(selected_dfs, ignore_index=True)
                sel_mode_nfc = normalize_nfc(selected_mode)
                
                if sel_mode_nfc == normalize_nfc(mode_seq):
                    # スプレッドシートの先頭から順に表示
                    target_df = target_df.reset_index(drop=True)
                elif sel_mode_nfc == normalize_nfc(mode_rand):
                    # ランダムに表示
                    target_df = target_df.sample(frac=1).reset_index(drop=True)
                elif sel_mode_nfc == normalize_nfc(mode_caution):
                    # 要注意の数字が大きいものから順に表示（同数の場合は元の順序を維持）
                    target_df = target_df.sort_values(
                        by='要注意', ascending=False, kind='mergesort'
                    ).reset_index(drop=True)
                    
                if len(target_df) == 0:
                    st.warning("選択した範囲の中に、条件に一致するデータがありません。")
                else:
                    with st.spinner("古い記録をリセットし、新しい表示順序を並列で記録しています..."):
                        client, _ = get_gspread_client()
                        target_df['出題順序'] = range(1, len(target_df) + 1)
                        
                        subj_order_dict = {subj: {} for subj in SPREADSHEET_KEYS.keys()}
                        for _, row in target_df.iterrows():
                            subj_order_dict[row['科目']][int(row['row_num'])] = int(row['出題順序'])
                            
                        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                            futures = {
                                executor.submit(
                                    _reset_order_col,
                                    subj,
                                    key,
                                    subj_order_dict[subj],
                                    st.session_state.sheet_col_meta.get(subj, {}),
                                    client
                                ): subj
                                for subj, key in SPREADSHEET_KEYS.items()
                            }
                            for future in concurrent.futures.as_completed(futures):
                                future.result()
                                
                        st.session_state.all_df['出題順序'] = 0
                        for subj, orders in subj_order_dict.items():
                            subj_nfc = normalize_nfc(subj)
                            for r, order in orders.items():
                                mask = (
                                    (st.session_state.all_df['科目'].apply(normalize_nfc) == subj_nfc) &
                                    (st.session_state.all_df['row_num'] == r)
                                )
                                st.session_state.all_df.loc[mask, '出題順序'] = order
                        
                    st.session_state.questions = target_df.to_dict('records')
                    st.session_state.current_idx = 0
                    st.session_state.pending_updates = {}
                    st.session_state.stage = 'quiz'
                    st.rerun()
                    
    with col2:
        if st.button("🔙 トップ画面に戻る", use_container_width=True):
            st.session_state.stage = 'home'
            st.rerun()

elif st.session_state.stage == 'quiz':
    total_q = len(st.session_state.questions)
    
    if total_q == 0:
        st.warning("⚠️ 再開できるデータが見つかりませんでした。（データがリセットされた可能性があります）")
        if st.button("トップ画面に戻る", type="primary"):
            st.session_state.stage = 'home'
            st.session_state.resume_info = None
            st.rerun()
    else:
        # 項目が切り替わるたびに画面上部へ自動スクロール
        components.html(
            f"""
            <!-- item_idx: {st.session_state.current_idx} -->
            <script>
                window.parent.document.querySelector('.main').scrollTop = 0;
            </script>
            """,
            height=0
        )
        current_q_num = st.session_state.current_idx + 1
        
        st.progress(current_q_num / total_q)
        st.caption(f"項目 {current_q_num} / {total_q}")
        
        # 学習中にも文字サイズ・強調設定を微調整できる折りたたみパネル
        with st.expander("🎨 文字サイズ・強調表示の設定を変更する", expanded=False):
            _render_style_settings_ui(key_prefix="quiz")

        current_q = st.session_state.questions[st.session_state.current_idx]
        
        base_px = int(st.session_state.base_font_size)
        hl_scale = float(st.session_state.highlight_scale)
        hl_px = int(round(base_px * hl_scale))
        hl_color = st.session_state.highlight_color
        highlight_cols_nfc = {normalize_nfc(c).strip() for c in st.session_state.highlight_cols}

        # 単元・分野タイトルも大きく見やすく表示
        subj_escaped = html.escape(str(current_q.get('科目', '')))
        cat_escaped = html.escape(str(current_q.get('分野', '')))
        st.markdown(
            f"<div style='font-size: {int(base_px * 1.1)}px; font-weight: bold; margin-bottom: 6px;'>"
            f"【{subj_escaped} - {cat_escaped}】</div>",
            unsafe_allow_html=True
        )
        if int(current_q.get('要注意', 0)) > 0:
            st.markdown(
                f"<div style='font-size: {max(14, int(base_px * 0.8))}px; color: #d97706; margin-bottom: 10px;'>"
                f"※現在の要注意カウント: {int(current_q['要注意'])}</div>",
                unsafe_allow_html=True
            )
            
        # 「要注意」ラベル列以外の各列を「ラベル：内容」として固定表示
        # 選択された強調ラベル列（語呂合わせ等）は指定倍率の文字サイズ＆指定色で表示
        display_cols = current_q.get('_display_cols', [])
        html_blocks = []
        for col_name in display_cols:
            cell_val = current_q.get(col_name, "")
            if pd.isna(cell_val):
                cell_val = ""
            col_escaped = html.escape(str(col_name))
            val_escaped = html.escape(str(cell_val)).replace("\n", "<br>")

            # NFC正規化で強調対象ラベル列かどうかを判定（例: 「語呂合わせ」や同名列の「語呂合わせ_2」等にも対応）
            col_nfc = normalize_nfc(col_name).strip()
            base_col_nfc = col_nfc.rsplit('_', 1)[0] if '_' in col_nfc and col_nfc.rsplit('_', 1)[1].isdigit() else col_nfc
            is_highlighted = (col_nfc in highlight_cols_nfc) or (base_col_nfc in highlight_cols_nfc)

            if is_highlighted:
                html_blocks.append(
                    f"<div style='margin-bottom: 18px; line-height: 1.6;'>"
                    f"<span style='font-size: {base_px}px; font-weight: bold; color: #1e293b;'>{col_escaped}：</span>"
                    f"<span style='font-size: {hl_px}px; font-weight: bold; color: {hl_color};'>{val_escaped}</span>"
                    f"</div>"
                )
            else:
                html_blocks.append(
                    f"<div style='margin-bottom: 16px; line-height: 1.6; font-size: {base_px}px; color: #1e293b;'>"
                    f"<span style='font-weight: bold;'>{col_escaped}：</span>"
                    f"<span>{val_escaped}</span>"
                    f"</div>"
                )
            
        if html_blocks:
            card_html = (
                "<div style='background-color: #f0f6ff; border-left: 6px solid #3b82f6; "
                "padding: 28px 32px; border-radius: 12px; margin-top: 8px; margin-bottom: 24px; "
                "box-shadow: 0 2px 6px rgba(0, 0, 0, 0.05);'>"
                + "".join(html_blocks)
                + "</div>"
            )
            st.markdown(card_html, unsafe_allow_html=True)
        else:
            st.warning("表示可能なラベル列が見つかりません。")

        def advance_item(add_caution=False):
            row = int(current_q['row_num'])
            subj = current_q['科目']
            current_caution_count = int(current_q.get('要注意', 0))
            
            if subj not in st.session_state.pending_updates:
                st.session_state.pending_updates[subj] = {}
                
            if add_caution:
                current_caution_count += 1
                st.session_state.questions[st.session_state.current_idx]['要注意'] = current_caution_count
                st.session_state.pending_updates[subj][row] = {
                    'caution': current_caution_count,
                    'order': ""
                }
            else:
                # 「次に進む」の場合は要注意の数値は変えず（初期状態の空白は維持）、出題順序のみクリア
                st.session_state.pending_updates[subj][row] = {
                    'order': ""
                }
                
            with st.spinner("記録を保存しています..."):
                save_learning_records()
                
            if current_q_num < total_q:
                st.session_state.current_idx += 1
            else:
                st.session_state.stage = 'result'

        col1, col2 = st.columns(2)
        with col1:
            if st.button("次に進む ➔", type="primary", use_container_width=True):
                advance_item(add_caution=False)
                st.rerun()
        with col2:
            if st.button("⚠️ 要注意にして次に進む", use_container_width=True):
                advance_item(add_caution=True)
                st.rerun()

        st.markdown("---")
        if st.button("⏸️ 中断してトップ画面に戻る", use_container_width=True):
            if st.session_state.pending_updates:
                with st.spinner("記録を保存して終了しています..."):
                    save_learning_records()
            st.session_state.stage = 'home'
            st.rerun()

elif st.session_state.stage == 'result':
    st.balloons()
    st.subheader("お疲れ様でした！全項目の確認が完了しました。")
    
    if st.button("トップ画面に戻る"):
        st.session_state.stage = 'home'
        st.rerun()
