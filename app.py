import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import concurrent.futures
import os

# --- ページ設定 ---
st.set_page_config(page_title="正誤クイズアプリ", page_icon="📝", layout="centered")

# --- Googleスプレッドシート連携の設定 ---
CREDENTIALS_FILE = "secret_key.json"

SPREADSHEET_KEYS = {
    "特許・実用新案": "1rrzp90pSYL4YxSJEEtkFB4HZsIi9n3SUsoUX1qEN5XQ", 
    "意匠": "1hOTVzkr82pqjh_KaoHxV7Zd-dczFtlVdu_DMT-FvNVM",
    "商標": "1I0HMXWY6CU9mQJWEBdJXn47Xv2_ErdLdTSZ1xxm3AG4",
    "協定・条約": "1fH1naJGQfKXseXpHrVFnC9ip93YURgCMD67xPhbUEjQ",
    "不正競争・著作権": "1w9AITUq-3HF53slPwFr83jOhws3kmVVy6dOfqHFHN9c"
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
if 'answered' not in st.session_state:
    st.session_state.answered = False
if 'is_correct' not in st.session_state:
    st.session_state.is_correct = False
if 'pending_updates' not in st.session_state:
    st.session_state.pending_updates = {}

# ==========================================
# 並列処理用ヘルパー関数群
# ==========================================
def _check_single_sheet_resume(subj, key, client):
    try:
        ws = client.open_by_key(key).sheet1
        f_col = ws.col_values(6)
        count = sum(1 for val in f_col[1:] if str(val).strip().isdigit() and int(val) > 0)
        return subj, count
    except Exception as e:
        return subj, e

def _load_single_sheet_data(subj, key, client):
    try:
        ws = client.open_by_key(key).sheet1
        data = ws.get_all_values()
        if len(data) > 1:
            # ヘッダーの空白を削除（表記揺れ対策）
            headers = [str(h).strip() for h in data[0]]
            df = pd.DataFrame(data[1:], columns=headers)
            
            # 1行目が空欄の場合の自動修復機能（E列=間違えた回数, F列=出題順序）
            col_names = list(df.columns)
            if len(col_names) >= 5 and col_names[4] == "": col_names[4] = "間違えた回数"
            if len(col_names) >= 6 and col_names[5] == "": col_names[5] = "出題順序"
            df.columns = col_names
            
            if '子フォルダ名' in df.columns: df = df.rename(columns={'子フォルダ名': '分野'})
            if '解答' in df.columns: df = df.rename(columns={'解答': '答え'})
            if '問題' in df.columns: df = df[df['問題'].astype(str).str.strip() != '']
            if '間違えた回数' not in df.columns: df['間違えた回数'] = 0
            if '出題順序' not in df.columns: df['出題順序'] = 0
            
            df['間違えた回数'] = pd.to_numeric(df['間違えた回数'], errors='coerce').fillna(0).astype(int)
            df['出題順序'] = pd.to_numeric(df['出題順序'], errors='coerce').fillna(0).astype(int)
            df['row_num'] = df.index + 2
            df['科目'] = subj
            return subj, df
        return subj, pd.DataFrame()
    except Exception as e:
        return subj, e

def _save_single_sheet(subj, key, updates, client):
    try:
        ws = client.open_by_key(key).sheet1
        max_row = max(updates.keys())
        cells = ws.range(f'E2:F{max_row}')
        for cell in cells:
            r = cell.row
            if r in updates:
                if cell.col == 5 and 'wrong' in updates[r]:
                    cell.value = updates[r]['wrong']
                elif cell.col == 6 and 'order' in updates[r]:
                    cell.value = updates[r]['order']
        ws.update_cells(cells)
        return subj, True
    except Exception as e:
        return subj, e

def _reset_f_col(subj, key, orders, client):
    try:
        ws = client.open_by_key(key).sheet1
        b_col = ws.col_values(2)
        num_rows = len(b_col)
        if num_rows > 1:
            f_cells = ws.range(f'F2:F{num_rows}')
            for cell in f_cells:
                r = cell.row
                cell.value = orders.get(r, 0)
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
            if isinstance(result, pd.DataFrame):
                if not result.empty:
                    all_data.append(result)
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
        futures = {executor.submit(_save_single_sheet, subj, SPREADSHEET_KEYS[subj], updates, client): subj 
                   for subj, updates in st.session_state.pending_updates.items() if updates}
        for future in concurrent.futures.as_completed(futures):
            subj, result = future.result()
            if not isinstance(result, bool):
                st.error(f"保存中にエラー ({subj}): {result}")

    for subj, updates in st.session_state.pending_updates.items():
        for r, vals in updates.items():
            mask = (st.session_state.all_df['科目'] == subj) & (st.session_state.all_df['row_num'] == r)
            if 'wrong' in vals:
                st.session_state.all_df.loc[mask, '間違えた回数'] = vals['wrong']
            if 'order' in vals:
                st.session_state.all_df.loc[mask, '出題順序'] = vals['order']
            
    st.session_state.pending_updates = {}
    st.session_state.resume_info = None

# --- メインUI ---
st.title("📝 正誤クイズ学習アプリ")

if st.session_state.stage == 'home':
    if st.session_state.resume_info is None:
        with st.spinner("過去の学習記録を高速スキャンしています..."):
            st.session_state.resume_info = check_resume_state()
            
    resume_info = st.session_state.resume_info
    
    if resume_info['count'] > 0:
        st.success(f"💡 前回中断したセッションの続き（残り {resume_info['count']}問）があります。")
        if st.button("▶️ 前回の続きから再開する", type="primary", use_container_width=True):
            with st.spinner("続きの問題データを並列で高速読み込みしています..."):
                load_data(resume_info['subjects'])
                df = st.session_state.all_df
                resume_df = df[df['出題順序'] > 0].sort_values(by='出題順序')
                
                st.session_state.questions = resume_df.to_dict('records')
                st.session_state.current_idx = 0
                st.session_state.answered = False
                st.session_state.pending_updates = {}
                st.session_state.stage = 'quiz'
                st.rerun()
        st.markdown("---")
        
    st.subheader("新規クイズを作成する")
    st.caption("※新しいクイズを作成すると、前回の続きは破棄されます。")
    if st.button("🆕 新規クイズの設定画面へ進む"):
        with st.spinner("全科目の出題データを並列で高速読み込みしています..."):
            load_data(list(SPREADSHEET_KEYS.keys()))
            st.session_state.stage = 'setup'
            st.rerun()

elif st.session_state.stage == 'setup':
    st.subheader("新規クイズの出題範囲設定")
    st.write("各科目から出題したい単元を選んでください。（全問題を選ぶ場合は、全科目の「全単元を出題する」にチェックを入れます）")
    
    all_df = st.session_state.all_df
    selected_dfs = []
    
    for subj in SPREADSHEET_KEYS.keys():
        with st.expander(f"📚 {subj}", expanded=False):
            if all_df.empty: continue
            
            subj_df = all_df[all_df['科目'] == subj]
            if subj_df.empty:
                st.write("データがありません。")
                continue
                
            categories = list(subj_df['分野'].dropna().unique())
            
            use_all = st.checkbox(f"「{subj}」の全単元を出題する", key=f"chk_{subj}")
            if use_all:
                selected_dfs.append(subj_df)
            else:
                sel_cats = st.multiselect(
                    "単元を選択（未選択の場合はこの科目からは出題されません）", 
                    options=categories, 
                    key=f"sel_{subj}"
                )
                if sel_cats:
                    selected_dfs.append(subj_df[subj_df['分野'].isin(sel_cats)])
    
    st.markdown("---")
    modes = ["すべて（ランダム出題）", "一度でも間違えた問題（ランダム）", "間違いが多い順（苦手克服）"]
    selected_mode = st.radio("出題モードを選択", modes)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("新規クイズスタート！", type="primary"):
            if not selected_dfs:
                st.warning("出題する単元が1つも選択されていません。")
            else:
                target_df = pd.concat(selected_dfs, ignore_index=True)
                
                if selected_mode == "一度でも間違えた問題（ランダム）":
                    target_df = target_df[target_df['間違えた回数'] > 0]
                    target_df = target_df.sample(frac=1).reset_index(drop=True)
                elif selected_mode == "間違いが多い順（苦手克服）":
                    target_df = target_df[target_df['間違えた回数'] > 0]
                    target_df = target_df.sort_values(by='間違えた回数', ascending=False).reset_index(drop=True)
                else:
                    target_df = target_df.sample(frac=1).reset_index(drop=True)
                    
                if len(target_df) == 0:
                    st.warning("選択した範囲の中に、条件に一致する問題がありません。")
                else:
                    with st.spinner("古い記録をリセットし、新しい出題順序を並列で記録しています..."):
                        client, _ = get_gspread_client()
                        target_df['出題順序'] = range(1, len(target_df) + 1)
                        
                        subj_order_dict = {subj: {} for subj in SPREADSHEET_KEYS.keys()}
                        for _, row in target_df.iterrows():
                            subj_order_dict[row['科目']][row['row_num']] = row['出題順序']
                            
                        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                            futures = {executor.submit(_reset_f_col, subj, key, subj_order_dict[subj], client): subj for subj, key in SPREADSHEET_KEYS.items()}
                            for future in concurrent.futures.as_completed(futures):
                                future.result()
                                
                        st.session_state.all_df['出題順序'] = 0
                        for subj, orders in subj_order_dict.items():
                            for r, order in orders.items():
                                mask = (st.session_state.all_df['科目'] == subj) & (st.session_state.all_df['row_num'] == r)
                                st.session_state.all_df.loc[mask, '出題順序'] = order
                        
                    st.session_state.questions = target_df.to_dict('records')
                    st.session_state.current_idx = 0
                    st.session_state.answered = False
                    st.session_state.pending_updates = {}
                    st.session_state.stage = 'quiz'
                    st.rerun()
                    
    with col2:
        if st.button("🔙 トップ画面に戻る"):
            st.session_state.stage = 'home'
            st.rerun()

elif st.session_state.stage == 'quiz':
    total_q = len(st.session_state.questions)
    
    # ゼロディビジョンエラーの完全防御処理
    if total_q == 0:
        st.warning("⚠️ 再開できる問題データが見つかりませんでした。（データがリセットされた可能性があります）")
        if st.button("トップ画面に戻る", type="primary"):
            st.session_state.stage = 'home'
            st.session_state.resume_info = None
            st.rerun()
    else:
        current_q_num = st.session_state.current_idx + 1
        
        st.progress(current_q_num / total_q)
        st.caption(f"問題 {current_q_num} / {total_q}")
        
        current_q = st.session_state.questions[st.session_state.current_idx]
        
        st.markdown(f"**【{current_q['科目']} - {current_q['分野']}】**")
        st.subheader(current_q['問題'])
        
        def check_answer(user_ans):
            correct_ans = str(current_q['答え']).strip()
            is_correct = (user_ans == correct_ans)
            st.session_state.is_correct = is_correct
            
            row = int(current_q['row_num'])
            subj = current_q['科目']
            current_wrong_count = int(current_q['間違えた回数'])
            
            if not is_correct:
                current_wrong_count += 1
                st.session_state.questions[st.session_state.current_idx]['間違えた回数'] = current_wrong_count
                
            if subj not in st.session_state.pending_updates:
                st.session_state.pending_updates[subj] = {}
                
            st.session_state.pending_updates[subj][row] = {
                'wrong': current_wrong_count,
                'order': 0 
            }
                
            st.session_state.answered = True

        if not st.session_state.answered:
            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("正しい ⭕️", use_container_width=True):
                    check_answer("正しい")
                    st.rerun()
            with col2:
                if st.button("誤り ❌", use_container_width=True):
                    check_answer("誤り")
                    st.rerun()
            with col3:
                if st.button("わからない 🤷", use_container_width=True):
                    check_answer("わからない")
                    st.rerun()
                    
        else:
            if st.session_state.is_correct:
                st.success("🎉 正解！")
            else:
                st.error("💦 不正解...")
                st.caption(f"※この問題を間違えた回数: {current_q['間違えた回数']}回")
                
            st.info(f"**【解説】**\n\n{current_q['解説']}")
            
            if current_q_num < total_q:
                if st.button("次の問題へ ➔", type="primary"):
                    st.session_state.current_idx += 1
                    st.session_state.answered = False
                    st.rerun()
            else:
                if st.button("結果画面へ", type="primary"):
                    st.session_state.stage = 'result'
                    st.rerun()

        st.markdown("---")
        if st.button("⏸️ 中断して経過を保存", use_container_width=True):
            if st.session_state.pending_updates:
                with st.spinner("学習記録を超高速で保存しています..."):
                    save_learning_records()
            st.session_state.stage = 'home'
            st.rerun()

elif st.session_state.stage == 'result':
    st.balloons()
    st.subheader("お疲れ様でした！全問終了です。")
    
    if st.session_state.pending_updates:
        with st.spinner("学習記録を超高速で保存しています..."):
            save_learning_records()
        st.success("すべての学習記録が保存されました！")
        
    if st.button("トップ画面に戻る"):
        st.session_state.stage = 'home'
        st.rerun()
