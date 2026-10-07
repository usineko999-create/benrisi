import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

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
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    if "gcp_service_account" in st.secrets:
        creds_dict = json.loads(st.secrets["gcp_service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds)

# --- セッション状態の初期化 ---
if 'stage' not in st.session_state:
    st.session_state.stage = 'setup'
if 'data_loaded' not in st.session_state:
    st.session_state.data_loaded = False
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
# データの全ロード処理（直列・安全版）
# ==========================================
def load_all_data():
    client = get_gspread_client()
    all_data = []
    
    for subj, key in SPREADSHEET_KEYS.items():
        try:
            ws = client.open_by_key(key).sheet1
            df = pd.DataFrame(ws.get_all_records())
            if df.empty: continue
            
            if '子フォルダ名' in df.columns: df = df.rename(columns={'子フォルダ名': '分野'})
            if '解答' in df.columns: df = df.rename(columns={'解答': '答え'})
                
            if '間違えた回数' not in df.columns: df['間違えた回数'] = 0
            if '出題順序' not in df.columns: df['出題順序'] = 0
            
            df['間違えた回数'] = pd.to_numeric(df['間違えた回数'], errors='coerce').fillna(0).astype(int)
            df['出題順序'] = pd.to_numeric(df['出題順序'], errors='coerce').fillna(0).astype(int)
            
            df['row_num'] = df.index + 2
            df['科目'] = subj
            all_data.append(df)
        except Exception as e:
            st.error(f"「{subj}」のデータ読み込みに失敗しました: {e}")
            
    if all_data:
        st.session_state.all_df = pd.concat(all_data, ignore_index=True)
    else:
        st.session_state.all_df = pd.DataFrame()
    st.session_state.data_loaded = True

# ==========================================
# 学習記録の保存処理（安全な保存 ＆ メモリ内直接更新）
# ==========================================
def save_learning_records():
    if not st.session_state.pending_updates:
        return
        
    client = get_gspread_client()
    
    # 1. スプレッドシートへ確実に保存
    for subj, updates in st.session_state.pending_updates.items():
        if not updates: continue
        try:
            ws = client.open_by_key(SPREADSHEET_KEYS[subj]).sheet1
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
        except Exception as e:
            print(f"Update error in {subj}: {e}")

    # 2. アプリ内メモリも書き換える（読み込みカットで一瞬で戻る）
    for subj, updates in st.session_state.pending_updates.items():
        for r, vals in updates.items():
            mask = (st.session_state.all_df['科目'] == subj) & (st.session_state.all_df['row_num'] == r)
            if 'wrong' in vals:
                st.session_state.all_df.loc[mask, '間違えた回数'] = vals['wrong']
            if 'order' in vals:
                st.session_state.all_df.loc[mask, '出題順序'] = vals['order']
                
    st.session_state.pending_updates = {}

# --- メインUI ---
st.title("📝 正誤クイズ学習アプリ")

if not st.session_state.data_loaded:
    with st.spinner("全科目の問題データを読み込んでいます..."):
        load_all_data()

# ==========================================
# トップ画面：設定画面 (stage: setup)
# ==========================================
if st.session_state.stage == 'setup':
    all_df = st.session_state.all_df
    
    if not all_df.empty:
        resume_df = all_df[all_df['出題順序'] > 0].copy()
        if len(resume_df) > 0:
            st.success(f"💡 前回中断したセッションの続き（全科目合計：残り {len(resume_df)}問）があります。")
            if st.button("▶️ 前回の続きから再開する", type="primary", use_container_width=True):
                resume_df = resume_df.sort_values(by='出題順序')
                st.session_state.questions = resume_df.to_dict('records')
                st.session_state.current_idx = 0
                st.session_state.answered = False
                st.session_state.pending_updates = {}
                st.session_state.stage = 'quiz'
                st.rerun()
            st.markdown("---")
    
    st.subheader("新規クイズの出題範囲設定")
    st.write("各科目から出題したい単元を選んでください。（全問題を選ぶ場合は、全科目の「全単元を出題する」にチェックを入れます）")
    
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
    
    if st.button("新規クイズスタート！", type="primary"):
        if not selected_dfs:
            st.warning("出題する単元が1つも選択されていません。少なくとも1つの科目で単元を選択するか、全単元にチェックを入れてください。")
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
                with st.spinner("古い記録をリセットし、新しい出題順序を記録しています..."):
                    client = get_gspread_client()
                    target_df['出題順序'] = range(1, len(target_df) + 1)
                    
                    subj_order_dict = {subj: {} for subj in SPREADSHEET_KEYS.items()}
                    for _, row in target_df.iterrows():
                        subj_order_dict[row['科目']][row['row_num']] = row['出題順序']
                        
                    for subj, key in SPREADSHEET_KEYS.items():
                        try:
                            ws = client.open_by_key(key).sheet1
                            b_col = ws.col_values(2)
                            num_rows = len(b_col)
                            if num_rows > 1:
                                f_cells = ws.range(f'F2:F{num_rows}')
                                orders = subj_order_dict[subj]
                                for cell in f_cells:
                                    r = cell.row
                                    cell.value = orders.get(r, 0)
                                ws.update_cells(f_cells)
                        except Exception:
                            pass
                    
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

# ==========================================
# クイズ画面 (stage: quiz)
# ==========================================
elif st.session_state.stage == 'quiz':
    total_q = len(st.session_state.questions)
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
            with st.spinner("学習記録を保存しています..."):
                save_learning_records()
        st.session_state.stage = 'setup'
        st.rerun()

# ==========================================
# 結果画面 (stage: result)
# ==========================================
elif st.session_state.stage == 'result':
    st.balloons()
    st.subheader("お疲れ様でした！全問終了です。")
    
    if st.session_state.pending_updates:
        with st.spinner("学習記録を保存しています..."):
            save_learning_records()
        st.success("すべての学習記録が保存されました！")
        
    if st.button("トップ画面に戻る"):
        st.session_state.stage = 'setup'
        st.rerun()
