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
    "不正競争・著作権": "1w9AITUq-3HF53slPwFr83jOhws3kmVVy6dOfqHFHN9c",
    "全問題": "1NHaNYmOv9TOXdmaDmWbVmIa3RFsXkXhVUYkFgASHtTk"
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
    st.session_state.stage = 'select_subject' 
if 'df' not in st.session_state:
    st.session_state.df = None
if 'worksheet' not in st.session_state:
    st.session_state.worksheet = None
if 'selected_subject_name' not in st.session_state:
    st.session_state.selected_subject_name = None
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
# 複数ファイル間の同期保存処理
# ==========================================
def save_learning_records():
    if not st.session_state.pending_updates:
        return
        
    client = get_gspread_client()
    current_subj = st.session_state.selected_subject_name
    current_key = SPREADSHEET_KEYS[current_subj]
    
    # 1. 現在学習中のファイルを更新（間違えた回数の更新 ＆ 解答済みの出題順序を0にクリア）
    current_ws = client.open_by_key(current_key).sheet1
    for q_text, data in st.session_state.pending_updates.items():
        current_ws.update_cell(data['row_num'], 5, data['wrong_count'])
        if data.get('clear_order'):
            current_ws.update_cell(data['row_num'], 6, 0)
            
    # 2. 他のファイルへ「間違えた回数」と「出題順序のクリア」を同期
    if current_subj != "全問題":
        try:
            sync_ws = client.open_by_key(SPREADSHEET_KEYS["全問題"]).sheet1
            q_col = sync_ws.col_values(2)
            for q_text, data in st.session_state.pending_updates.items():
                if q_text in q_col:
                    sync_row = q_col.index(q_text) + 1 
                    sync_ws.update_cell(sync_row, 5, data['wrong_count'])
                    # 【修正】同期先でも出題順序を必ず0に戻す
                    if data.get('clear_order'):
                        sync_ws.update_cell(sync_row, 6, 0)
        except Exception:
            pass 
    else:
        remaining = list(st.session_state.pending_updates.keys())
        for subj_name, subj_key in SPREADSHEET_KEYS.items():
            if subj_name == "全問題" or not remaining:
                continue
            try:
                sync_ws = client.open_by_key(subj_key).sheet1
                q_col = sync_ws.col_values(2)
                found = []
                for q_text in remaining:
                    if q_text in q_col:
                        sync_row = q_col.index(q_text) + 1
                        sync_ws.update_cell(sync_row, 5, st.session_state.pending_updates[q_text]['wrong_count'])
                        # 【修正】同期先でも出題順序を必ず0に戻す
                        if st.session_state.pending_updates[q_text].get('clear_order'):
                            sync_ws.update_cell(sync_row, 6, 0)
                        found.append(q_text)
                for f in found:
                    remaining.remove(f)
            except Exception:
                continue
                
    st.session_state.pending_updates = {}
    # トップ画面に戻った際に最新のセッション状態を再取得させるためキャッシュを削除
    st.session_state.pop('global_resume_df', None)

# --- メインUI ---
st.title("📝 正誤クイズ学習アプリ")

# ==========================================
# トップ画面：セッション確認 ＆ 科目選択 (stage: select_subject)
# ==========================================
if st.session_state.stage == 'select_subject':
    
    # 【新規】アプリ起動時、「全問題」ファイルから全セッションの続きがあるか確認
    if 'global_resume_df' not in st.session_state:
        with st.spinner("過去の学習記録を確認しています..."):
            client = get_gspread_client()
            zen_ws = client.open_by_key(SPREADSHEET_KEYS["全問題"]).sheet1
            zen_df = pd.DataFrame(zen_ws.get_all_records())
            
            if '解答' in zen_df.columns and '答え' not in zen_df.columns:
                zen_df = zen_df.rename(columns={'解答': '答え'})
            if '出題順序' not in zen_df.columns:
                zen_df['出題順序'] = 0
            else:
                zen_df['出題順序'] = pd.to_numeric(zen_df['出題順序'], errors='coerce').fillna(0).astype(int)
            if '間違えた回数' not in zen_df.columns:
                zen_df['間違えた回数'] = 0
            else:
                zen_df['間違えた回数'] = pd.to_numeric(zen_df['間違えた回数'], errors='coerce').fillna(0).astype(int)
                
            zen_df['row_num'] = zen_df.index + 2
            st.session_state.zen_ws = zen_ws
            # 出題順序が1以上の問題だけを抽出して保存
            st.session_state.global_resume_df = zen_df[zen_df['出題順序'] > 0].copy()

    # 続きがある場合、トップ画面の最上部に再開ボタンを表示
    resume_df = st.session_state.global_resume_df
    if len(resume_df) > 0:
        st.success(f"💡 前回中断したセッションの続き（残り {len(resume_df)}問）があります。")
        if st.button("▶️ 前回の続きから再開する", type="primary", use_container_width=True):
            resume_df = resume_df.sort_values(by='出題順序')
            st.session_state.questions = resume_df.to_dict('records')
            st.session_state.current_idx = 0
            st.session_state.answered = False
            st.session_state.pending_updates = {}
            # 「全問題」シートをベースに再開
            st.session_state.selected_subject_name = "全問題"
            st.session_state.worksheet = st.session_state.zen_ws
            st.session_state.stage = 'quiz'
            st.rerun()
        st.markdown("---")
    
    st.subheader("1. 新規クイズの科目選択")
    st.caption("※下の「次へ」から進んで新しいクイズを開始すると、前回の続きは破棄されます。")
    
    selected_subject = st.radio(
        "学習する科目を選んでください", 
        list(SPREADSHEET_KEYS.keys()), 
        index=0
    )
    
    if st.button("次へ（データ読み込み）"):
        try:
            with st.spinner(f"「{selected_subject}」のデータを読み込んでいます..."):
                client = get_gspread_client()
                target_key = SPREADSHEET_KEYS[selected_subject]
                
                worksheet = client.open_by_key(target_key).sheet1
                data = worksheet.get_all_records()
                df = pd.DataFrame(data)
                
                if '子フォルダ名' in df.columns and '分野' not in df.columns:
                    df = df.rename(columns={'子フォルダ名': '分野'})
                if '解答' in df.columns and '答え' not in df.columns:
                    df = df.rename(columns={'解答': '答え'})
                    
                if '間違えた回数' not in df.columns:
                    df['間違えた回数'] = 0
                else:
                    df['間違えた回数'] = pd.to_numeric(df['間違えた回数'], errors='coerce').fillna(0).astype(int)
                
                if '出題順序' not in df.columns:
                    df['出題順序'] = 0
                else:
                    df['出題順序'] = pd.to_numeric(df['出題順序'], errors='coerce').fillna(0).astype(int)
                    
                df['row_num'] = df.index + 2 
                
                st.session_state.worksheet = worksheet
                st.session_state.df = df
                st.session_state.selected_subject_name = selected_subject
                st.session_state.stage = 'setup'
                st.rerun()
                
        except Exception as e:
            st.error(f"データの読み込みに失敗しました。\nエラー内容: {e}")

# ==========================================
# 設定画面 (stage: setup)
# ==========================================
elif st.session_state.stage == 'setup':
    st.subheader(f"2. 出題設定（{st.session_state.selected_subject_name}）")
    
    df = st.session_state.df
    worksheet = st.session_state.worksheet
    
    categories = list(df['分野'].dropna().unique())
    selected_categories = st.multiselect(
        "出題する範囲（単元）を選択してください", 
        options=categories,
        default=None,
        placeholder="クリックして文字検索・複数選択（未選択の場合は全範囲）"
    )
    
    modes = ["すべて（ランダム出題）", "一度でも間違えた問題（ランダム）", "間違いが多い順（苦手克服）"]
    selected_mode = st.radio("出題モードを選択", modes)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("新規クイズスタート！", type="primary"):
            target_df = df.copy()
            
            if len(selected_categories) > 0:
                target_df = target_df[target_df['分野'].isin(selected_categories)]
                
            if selected_mode == "一度でも間違えた問題（ランダム）":
                target_df = target_df[target_df['間違えた回数'] > 0]
                target_df = target_df.sample(frac=1).reset_index(drop=True)
            elif selected_mode == "間違いが多い順（苦手克服）":
                target_df = target_df[target_df['間違えた回数'] > 0]
                target_df = target_df.sort_values(by='間違えた回数', ascending=False).reset_index(drop=True)
            else:
                target_df = target_df.sample(frac=1).reset_index(drop=True)
                
            if len(target_df) == 0:
                st.warning("条件に一致する問題がありません。")
            else:
                # 【完全修正】新規スタート時は、全てのファイルの「出題順序」を強制的にリセットして上書きする
                with st.spinner("古い記録をリセットし、新しい出題順序を全ファイルに同期しています... (数秒かかります)"):
                    target_df['出題順序'] = range(1, len(target_df) + 1)
                    new_order_dict = {str(q['問題']).strip(): int(q['出題順序']) for q in target_df.to_dict('records')}
                    
                    client = get_gspread_client()
                    
                    for subj, key in SPREADSHEET_KEYS.items():
                        try:
                            ws = client.open_by_key(key).sheet1
                            b_col = ws.col_values(2) # B列: 問題
                            if not b_col: continue
                            
                            new_f_col = []
                            # 1行目(見出し)以降を処理
                            for idx in range(1, len(b_col)):
                                q_text = str(b_col[idx]).strip()
                                # 選ばれた問題には番号を、それ以外は0(リセット)を入れる
                                if q_text in new_order_dict:
                                    new_f_col.append(new_order_dict[q_text])
                                else:
                                    new_f_col.append(0)
                                    
                            if new_f_col:
                                cell_list = ws.range(f'F2:F{len(new_f_col)+1}')
                                for i, cell in enumerate(cell_list):
                                    cell.value = new_f_col[i]
                                ws.update_cells(cell_list)
                        except Exception:
                            continue
                
                st.session_state.questions = target_df.to_dict('records')
                st.session_state.current_idx = 0
                st.session_state.answered = False
                st.session_state.pending_updates = {}
                # スタート時にキャッシュを消しておく
                st.session_state.pop('global_resume_df', None)
                st.session_state.stage = 'quiz'
                st.rerun()
                
    with col2:
        if st.button("🔙 科目選択に戻る"):
            st.session_state.stage = 'select_subject'
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
    
    st.markdown(f"**【{current_q['分野']}】**")
    st.subheader(current_q['問題'])
    
    def check_answer(user_ans):
        correct_ans = str(current_q['答え']).strip()
        is_correct = (user_ans == correct_ans)
        st.session_state.is_correct = is_correct
        
        row = int(current_q['row_num'])
        q_text = str(current_q['問題']).strip()
        current_wrong_count = int(current_q['間違えた回数'])
        
        if not is_correct:
            current_wrong_count += 1
            st.session_state.questions[st.session_state.current_idx]['間違えた回数'] = current_wrong_count
            
        st.session_state.pending_updates[q_text] = {
            'row_num': row, 
            'wrong_count': current_wrong_count,
            'clear_order': True # 解答済みとして順番を消去するフラグ
        }
            
        st.session_state.answered = True

    if not st.session_state.answered:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("正しい ⭕️", use_container_width=True):
                check_answer("正しい")
                st.rerun()
        with col2:
            if st.button("誤り ❌", use_container_width=True):
                check_answer("誤り")
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
            with st.spinner("ファイル間で学習記録を同期し、解答済みの出題順序を削除しています..."):
                save_learning_records()
        st.session_state.stage = 'select_subject'
        st.rerun()

# ==========================================
# 結果画面 (stage: result)
# ==========================================
elif st.session_state.stage == 'result':
    st.balloons()
    st.subheader("お疲れ様でした！全問終了です。")
    
    if st.session_state.pending_updates:
        with st.spinner("ファイル間で学習記録を同期し、出題順序をクリアしています..."):
            save_learning_records()
        st.success("すべての学習記録が保存・同期されました！")
        
    if st.button("トップ画面に戻る"):
        st.session_state.stage = 'select_subject'
        st.rerun()
