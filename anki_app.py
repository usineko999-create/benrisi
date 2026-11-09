import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import concurrent.futures
import os
import unicodedata
import streamlit.components.v1 as components

# --- 文字列比較用のNFC正規化ヘルパー関数 ---
def normalize_nfc(text):
    """比較・検索・グループ化用に文字列をNFC正規化する（ファイル操作には使用しない）"""
    if text is None:
        return ""
    return unicodedata.normalize('NFC', str(text))

# --- ページ設定 ---
st.set_page_config(page_title="暗記リストアプリ", page_icon="📖", layout="centered")

# --- Googleスプレッドシート連携の設定 ---
CREDENTIALS_FILE = "secret_key.json"

# 【重要】ここの単元名と文字列を、ご自身のスプレッドシートのキーに書き換えてください
SPREADSHEET_KEYS = {
    "語呂合わせ_特許・実用新案": "1GOA12mzr6yWzm1Cdu-TMmnQdZ_Y8goV5I5MT9IcPKLw",
    
}

@st.cache_resource
def get_gspread_client():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        
        if "gcp_service_account" in st.secrets:
            try:
                creds_dict = json.loads(st.secrets["gcp_service_account"])
            except Exception as e:
                return None, f"SecretsのJSON形式が間違っています。詳細: {e}"
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            
        elif os.path.exists(CREDENTIALS_FILE):
            # ファイル操作には正規化前の元のパス文字列(CREDENTIALS_FILE)をそのまま使用
            creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
            
        else:
            return None, "【エラー】Streamlit Cloudの「Secrets」が設定されていません。Manage app > Settings > Secretsを開き、鍵を保存してください。"
            
        return gspread.authorize(creds), None
    except Exception as e:
        return None, f"認証処理中にエラーが発生しました: {str(e)}"

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
    
    # 「要注意」列の特定（見つからない場合は5列目をデフォルトとする）
    caution_col_idx = None
    for idx, nh in enumerate(norm_headers):
        if nh == caution_target or nh == wrong_target:
            caution_col_idx = idx + 1
            break
    if caution_col_idx is None:
        caution_col_idx = 5

    # 「出題順序」列の特定（見つからない場合は「要注意」列の右隣とする）
    order_col_idx = None
    for idx, nh in enumerate(norm_headers):
        if nh == order_target:
            order_col_idx = idx + 1
            break
    if order_col_idx is None:
        order_col_idx = caution_col_idx + 1

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
        ws = client.open_by_key(key).sheet1
        data = ws.get_all_values()
        if len(data) > 1:
            raw_headers = [str(h).strip() for h in data[0]]
            caution_col_idx, order_col_idx, display_cols = _detect_sheet_columns(raw_headers)
            
            # 必要な列数分だけ各行の長さを揃える
            max_needed_cols = max(len(raw_headers), caution_col_idx, order_col_idx)
            while len(raw_headers) < max_needed_cols:
                raw_headers.append("")
                
            col_names = list(raw_headers)
            col_names[caution_col_idx - 1] = "要注意"
            col_names[order_col_idx - 1] = "出題順序"
            
            # 分野列の正規化判定と統一
            for idx, h in enumerate(col_names):
                nh = normalize_nfc(h)
                if nh == normalize_nfc("子フォルダ名") or nh == normalize_nfc("分野"):
                    col_names[idx] = "分野"
                    
            padded_rows = [
                r + [""] * (max_needed_cols - len(r)) if len(r) < max_needed_cols else r[:max_needed_cols]
                for r in data[1:]
            ]
            df = pd.DataFrame(padded_rows, columns=col_names)
            
            # 重複する空欄列名があれば除去
            df = df.loc[:, [c != "" for c in df.columns]]
            
            if '分野' not in df.columns and len(df.columns) > 0:
                df = df.rename(columns={df.columns[0]: '分野'})
                
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
            return subj, (df, meta)
        return subj, (pd.DataFrame(), {'caution_col': 5, 'order_col': 6, 'display_cols': []})
    except Exception as e:
        return subj, e

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
        st.error(f"⚠️ 【システムエラー】Google APIとの接続に失敗しました。\n詳細: {err}")
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
        st.error(f"⚠️ 接続エラー: {err}")
        return
        
    all_data = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_load_single_sheet_data, subj, SPREADSHEET_KEYS[subj], client): subj for subj in subjects_to_load}
        for future in concurrent.futures.as_completed(futures):
            subj, result = future.result()
            if isinstance(result, tuple):
                df, meta = result
                st.session_state.sheet_col_meta[subj] = meta
                if not df.empty:
                    all_data.append(df)
            else:
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
        st.error(f"保存エラー: {err}")
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
    
    all_df = st.session_state.all_df
    selected_dfs = []
    
    for subj in SPREADSHEET_KEYS.keys():
        with st.expander(f"📚 {subj}", expanded=False):
            if all_df.empty:
                st.write("データがありません。")
                continue
            
            subj_norm = normalize_nfc(subj)
            subj_df = all_df[all_df['科目'].apply(normalize_nfc) == subj_norm]
            if subj_df.empty:
                st.write("データがありません。")
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
    mode_seq = "先頭から順に表示"
    mode_rand = "ランダムに表示"
    mode_caution = "要注意のものから表示"
    modes = [mode_seq, mode_rand, mode_caution]
    selected_mode = st.radio("暗記リスト表示方法を選択", modes)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("暗記リストスタート！", type="primary"):
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
        if st.button("🔙 トップ画面に戻る"):
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
        
        current_q = st.session_state.questions[st.session_state.current_idx]
        
        st.markdown(f"**【{current_q['科目']} - {current_q['分野']}】**")
        if int(current_q.get('要注意', 0)) > 0:
            st.caption(f"※現在の要注意カウント: {int(current_q['要注意'])}")
            
        # 「要注意」ラベル列以外の各列を「ラベル：内容」として固定表示
        display_cols = current_q.get('_display_cols', [])
        display_lines = []
        for col_name in display_cols:
            cell_val = current_q.get(col_name, "")
            if pd.isna(cell_val):
                cell_val = ""
            display_lines.append(f"**{col_name}**：{cell_val}")
            
        if display_lines:
            st.info("  \n\n".join(display_lines))
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
