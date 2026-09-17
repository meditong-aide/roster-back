"""
엑셀 파일 처리 서비스
간호사 정보 엑셀 업로드/다운로드 관련 기능 제공
"""
import pandas as pd
import uuid
import tempfile
import os
import re
from typing import List, Dict, Any, Tuple, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from datetime import datetime

from db.models import Nurse as NurseModel, Group as GroupModel, Office as OfficeModel
from schemas.auth_schema import User as UserSchema
from db.client2 import msdb_manager
from datalayer.member import Member
from datalayer.setting import Setting
from utils.security import create_access_token
import requests


def create_nurse_template() -> str:
    """간호사 정보 엑셀 템플릿 생성"""
    
    # 템플릿 데이터 구조
    template_data = {
        '병동명': ['ICU', 'ICU', '응급실', '(입력 가이드)', ''],
        '식별코드': ['UUID자동생성', 'UUID자동생성', 'UUID자동생성', '(UUID는 자동생성됨)', ''],
        '계정 ID': ['nurse001', 'nurse002', 'nurse003', '(영문숫자조합)', ''],
        '이름': ['김간호', '이수간', '박일반', '(한글이름)', ''],
        '경력': [5, 10, 3, '(1이상정수)', ''],
        '직군': ['간호사', '간호사', '간호사', '(간호사)', ''],
        '직책': ['주임', '수간호사', '일반', '(주임/수간호사/일반)', ''],
        '수간호사여부': ['N', 'Y', 'N', '(Y/N)', '']
    }
    
    # DataFrame 생성
    df = pd.DataFrame(template_data)
    
    # 임시 파일 생성
    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
        template_path = tmp_file.name
    
    # 엑셀 파일로 저장
    with pd.ExcelWriter(template_path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='간호사정보', index=False)
        
        # 워크시트 스타일링
        worksheet = writer.sheets['간호사정보']
        
        # 헤더 스타일 적용
        for col in range(1, len(df.columns) + 1):
            cell = worksheet.cell(row=1, column=col)
            cell.font = cell.font.copy(bold=True)
            cell.fill = cell.fill.copy(fgColor="CCCCCC")
        
        # 가이드 행 스타일 적용 (4번째 행)
        for col in range(1, len(df.columns) + 1):
            cell = worksheet.cell(row=4, column=col)
            cell.font = cell.font.copy(italic=True, color="666666")
    
    return template_path
def create_nurse_template2() -> str:
    """엑셀 템플릿2: 계정ID/이름 두 컬럼만 포함."""
    template_data = {
        '사번(필수)': ['1001', '1002', '1003', '(영문숫자조합)'],
        '계정 ID(필수)': ['nurse001', 'nurse002', 'nurse003', '(영문숫자조합)'],
        '직원명(필수)': ['김수간', '이간호', '최간호', '(한글이름)'],
        '직무(필수)': ['HN', 'AN', 'RN', ('직무코드')],
        '경력(필수)': [25, 15, 1, ('경력년수')],
        '수간호사여부(필수)': ['Y', 'N', 'N', ('수간호사여부 정보')],
        '입사일(선택)': ['2025-01-03', '', '', ('입사일 정보')],
        # '적용해제일(선택)': ['2025-01-25', '', '', ('근무 표 적용해제일 정보')],
        '생년월일(필수)': ['1999-01-01', '', '', ('생년월일 정보')],
        '연락처(필수)': ['010-0000-0000', '', '', ('연락처 정보')],
        '성별(필수)': ['남', '', '', ('성별 정보')],
        '이메일(선택)': ['nurse001@hospital.com', '', '', ('이메일 정보')],

    }
    df = pd.DataFrame(template_data)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
        template_path = tmp_file.name
    with pd.ExcelWriter(template_path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='업로드2', index=False)
        ws = writer.sheets['업로드2']
        for col in range(1, len(df.columns) + 1):
            cell = ws.cell(row=1, column=col)
            cell.font = cell.font.copy(bold=True)
    return template_path



def get_or_create_group(group_name: str, user: UserSchema, db: Session) -> Tuple[Optional[str], bool, List[str]]:
    """
    병동명을 기반으로 group_id를 찾거나 새로 생성
    Returns: (group_id, is_new_group, warnings)
    """
    warnings = []
    
    # 현재 사용자의 office_id로 해당 office의 모든 그룹 조회
    existing_group = db.query(GroupModel).filter(
        GroupModel.office_id == user.office_id,
        GroupModel.group_name == group_name
    ).first()
    
    if existing_group:
        warnings.append(f"기존 '{group_name}' 그룹에 추가됩니다.")
        return existing_group.group_id, False, warnings
    
    # 기존에 없는 그룹명인 경우 새 그룹 ID 생성
    new_group_id = generate_new_group_id(user.office_id, db)
    warnings.append(f"새로운 그룹 '{group_name}'이 생성됩니다.")
    return new_group_id, True, warnings


def generate_new_group_id(office_id: str, db: Session) -> str:
    """office_id + 001, 002, 003... 형태로 새 그룹 ID 생성"""
    
    # 해당 office의 기존 그룹들 중 최대 번호 찾기
    existing_groups = db.query(GroupModel).filter(
        GroupModel.office_id == office_id,
        GroupModel.group_id.like(f"{office_id}%")
    ).all()
    
    max_number = 0
    for group in existing_groups:
        try:
            # office_id 뒤의 숫자 부분 추출
            number_part = group.group_id.replace(office_id, "")
            if number_part.isdigit():
                max_number = max(max_number, int(number_part))
        except:
            continue
    
    # 다음 번호로 새 그룹 ID 생성
    new_number = max_number + 1
    return f"{office_id}{new_number:03d}"


def create_new_group(group_name: str, group_id: str, user: UserSchema, db: Session) -> str:
    """새로운 그룹 생성"""
    
    new_group = GroupModel(
        group_id=group_id,
        office_id=user.office_id,
        group_name=group_name
    )
    
    db.add(new_group)
    db.flush()  # DB에 즉시 반영하되 커밋은 나중에
    
    return group_id


def get_next_sequence(group_id: str, active_status: int, db: Session, role: str = "RN") -> int:
    """해당 그룹의 특정 active 상태 + role 그룹에서 다음 sequence 번호 반환"""
    from services.nurse_service import get_role_group, _role_group_filter

    role_group = get_role_group(role)
    max_sequence = db.query(func.max(NurseModel.sequence)).filter(
        NurseModel.group_id == group_id,
        NurseModel.active == active_status,
        _role_group_filter(role_group)
    ).scalar()

    return (max_sequence or 0) + 1


# def process_excel_upload(file_path: str, user: UserSchema, db: Session) -> Dict[str, Any]:
#     """엑셀 파일 업로드 처리 및 검증"""
    
#     try:
#         # 엑셀 파일 읽기
#         df = pd.read_excel(file_path, sheet_name=0)
        
#         # 빈 행 및 가이드 행 제거
#         df = df.dropna(how='all')  # 모든 컬럼이 비어있는 행 제거
#         df = df[~df.iloc[:, 0].astype(str).str.contains('입력 가이드', na=False)]  # 가이드 행 제거
        
#         # 최대 행 수 검증
#         if len(df) > 1000:
#             raise ValueError("최대 1000행까지만 업로드 가능합니다.")
#         # 컬럼 매핑
#         column_mapping = {
#             '병동명': 'group_name',
#             '식별코드': 'nurse_id', 
#             '계정 ID': 'account_id',
#             '이름': 'name',
#             '경력': 'experience',
#             '직군': 'role',
#             '직책': 'level_',
#             '수간호사여부': 'is_head_nurse',
#             '생년월일': 'birth_date',
#             '연락처': 'phone_number'
#         }
#         # 컬럼명 유연 매핑 (유사한 이름 인식)
#         flexible_mapping = {}
#         for excel_col in df.columns:
#             excel_col_clean = str(excel_col).strip()
#             for standard_col, db_field in column_mapping.items():
#                 if (excel_col_clean == standard_col or 
#                     excel_col_clean in ['병동', '부서'] and standard_col == '병동명' or
#                     excel_col_clean in ['ID', '아이디'] and standard_col == '계정 ID' or
#                     excel_col_clean in ['성명', '간호사명'] and standard_col == '이름' or
#                     excel_col_clean in ['년차', '경력년수'] and standard_col == '경력' or
#                     excel_col_clean in ['수간호사', '헤드너스'] and standard_col == '수간호사여부' or
#                     excel_col_clean in ['출생일', 'Birthday', '생일'] and standard_col == '생년월일' or
#                     excel_col_clean in ['전화번호', 'Phone', '휴대폰'] and standard_col == '연락처' or
#                     excel_col_clean in ['성별', 'Gender', '남/여'] and standard_col == '성별'):
#                     flexible_mapping[excel_col] = db_field
#                     break
#         # 필수 컬럼 확인
#         required_fields = ['group_name', 'account_id', 'name', 'experience', 'role', 'level_', 'is_head_nurse', 'birth_date', 'phone_number']
#         missing_fields = [field for field in required_fields if field not in flexible_mapping.values()]
#         if missing_fields:
#             missing_korean = []
#             field_korean_map = {
#                 'group_name': '병동명',
#                 'account_id': '계정 ID', 
#                 'name': '이름',
#                 'experience': '경력',
#                 'role': '직군',
#                 'level_': '직책',
#                 'is_head_nurse': '수간호사여부',
#                 'birth_date': '셍냔월일',
#                 'phone_number': '연락처'
#             }
#             for field in missing_fields:
#                 missing_korean.append(field_korean_map.get(field, field))
#             raise ValueError(f"필수 컬럼이 누락되었습니다: {', '.join(missing_korean)}")
            
#         # 병동명별 그룹 정보 수집
#         # 동일한 병동명이 여러 병원(office)에서 존재할 수 있으므로, 엑셀에 오피스/지점 컬럼이 존재하는 경우
#         # 먼저 현재 사용자 office_id와 일치하는 행으로 필터링한다.
#         office_col = None
#         for c in ['office_id', 'Office ID', '오피스ID', '병원ID', '병원코드', '기관ID', '지점ID']:
#             if c in df.columns:
#                 office_col = c
#                 break
#         if office_col:
#             df_filtered = df[df[office_col].astype(str).str.strip() == str(user.office_id)]
#         else:
#             df_filtered = df

#         unique_groups = df_filtered[get_excel_column_by_field('group_name', flexible_mapping)].dropna().unique()
#         group_info = {}
#         new_groups_needed = []
        
#         for group_name in unique_groups:
#             group_name = str(group_name).strip()
#             if not group_name:
#                 continue
                
#             group_id, is_new, warnings = get_or_create_group(group_name, user, db)
#             group_info[group_name] = {
#                 'group_id': group_id,
#                 'is_new': is_new,
#                 'warnings': warnings
#             }
            
#             if is_new:
#                 new_groups_needed.append(group_name)
        
#         # 그룹별 sequence 카운터 초기화 (활성 상태 기준)
#         group_sequence_counters = {}
#         for group_name, info in group_info.items():
#             group_id = info['group_id']
#             if info['is_new']:
#                 # 새 그룹인 경우 1부터 시작
#                 group_sequence_counters[group_id] = 1
#             else:
#                 # 기존 그룹인 경우 활성 상태(active=1)의 다음 sequence 가져오기
#                 group_sequence_counters[group_id] = get_next_sequence(group_id, 1, db)
        
#         # 데이터 변환
#         processed_data = []
#         validation_results = []
        
#         for idx, row in df.iterrows():
#             try:
#                 # 병동명 처리
#                 group_name = str(row[get_excel_column_by_field('group_name', flexible_mapping)]).strip()
#                 group_data = group_info.get(group_name, {})
#                 group_id = group_data.get('group_id')
                
#                 # sequence 할당
#                 sequence = group_sequence_counters.get(group_id, 0)
#                 group_sequence_counters[group_id] = sequence + 1
                
#                 # 기본 데이터 변환 (nurses.office_id 함께 저장)
#                 nurse_data = {
#                     # 'group_name': group_name,
#                     'group_id': group_id,
#                     'office_id': user.office_id,
#                     'nurse_id': str(uuid.uuid4()) if pd.isna(row.get('식별코드')) or str(row.get('식별코드')).strip() == 'UUID자동생성' else str(row.get('식별코드')),
#                     'account_id': str(row[get_excel_column_by_field('account_id', flexible_mapping)]).strip(),
#                     'name': str(row[get_excel_column_by_field('name', flexible_mapping)]).strip(),
#                     'experience': int(float(row[get_excel_column_by_field('experience', flexible_mapping)])),
#                     'role': str(row[get_excel_column_by_field('role', flexible_mapping)]).strip(),
#                     'level_': str(row[get_excel_column_by_field('level_', flexible_mapping)]).strip(),
#                     'is_head_nurse': parse_boolean(row[get_excel_column_by_field('is_head_nurse', flexible_mapping)]),
#                     # 'allowed_shifts': False,  # 기본값
#                     'allowed_shifts': [],  # 기본값
#                     'personal_off_adjustment': 0,  # 기본값
#                     'preceptor_id': None,  # 기본값
#                     'joining_date': None,  # 기본값
#                     'resignation_date': None,  # 기본값
#                     'sequence': sequence,
#                     'active': 1,  # 엑셀 업로드는 기본적으로 활성 상태
#                     'birth_date': str(row[get_excel_column_by_field('birth_date', flexible_mapping)]).strip() if get_excel_column_by_field('birth_date', flexible_mapping) in row and pd.notna(row[get_excel_column_by_field('birth_date', flexible_mapping)]) else None,  # 신규 컬럼: 생년월일
#                     'phone_number': str(row[get_excel_column_by_field('phone_number', flexible_mapping)]).strip() if get_excel_column_by_field('phone_number', flexible_mapping) in row and pd.notna(row[get_excel_column_by_field('phone_number', flexible_mapping)]) else None  # 신규 컬럼: 연락처
#                 }
                
#                 # 개별 행 검증
#                 row_validation = validate_single_row(group_name, nurse_data, user, db)
#                 print('row_validation!', row_validation)
                
#                 # 그룹 상태 정보 추가
#                 row_validation['warnings'].extend(group_data.get('warnings', []))
#                 row_validation['is_new_group'] = group_data.get('is_new', False)
#                 row_validation['group_name'] = group_name
                
#                 processed_data.append(nurse_data)
#                 validation_results.append(row_validation)
                
#             except Exception as e:
#                 # 행별 오류 처리
#                 error_data = {
#                     'row_index': idx + 2,  # 엑셀 행 번호 (헤더 포함)
#                     'error': str(e),
#                     'is_valid': False,
#                     'errors': [str(e)],
#                     'warnings': [],
#                     'is_new_group': False
#                 }
#                 validation_results.append(error_data)
#                 processed_data.append(None)
        
#         # 전체 검증 결과 요약
#         valid_count = sum(1 for result in validation_results if result.get('is_valid', False))
#         error_count = len(validation_results) - valid_count
#         overwrite_count = sum(1 for result in validation_results if result.get('is_overwrite', False))
#         new_group_count = len(new_groups_needed)
        
#         return {
#             'success': True,
#             'data': processed_data,
#             'validation_results': validation_results,
#             'new_groups_needed': new_groups_needed,
#             'summary': {
#                 'total': len(validation_results),
#                 'valid': valid_count,
#                 'error': error_count,
#                 'overwrite': overwrite_count,
#                 'new_groups': new_group_count
#             }
#         }
        
#     except Exception as e:
#         return {
#             'success': False,
#             'error': str(e),
#             'data': [],
#             'validation_results': [],
#             'new_groups_needed': [],
#             'summary': {'total': 0, 'valid': 0, 'error': 0, 'overwrite': 0, 'new_groups': 0}
#         }


def upload2_validate(file_path: str, user: UserSchema, db: Session, group_id: str) -> Dict[str, Any]:
    """업로드2: 파일을 검증만 수행하고, 정규화된 행과 오류를 반환한다.

    - 행별 오류: 포맷/타입/허용 계정/필수값 등
    - 전역 오류: 수간호사 최소 1명, DB 중복 계정 등
    """
    try:
        df = pd.read_excel(file_path, sheet_name=0)
        df = df.dropna(how='all')
        if len(df) > 2000:
            raise ValueError("최대 2000행까지만 업로드 가능합니다.")

        def find_col(candidates: list[str]) -> str:
            for c in df.columns:
                cc = str(c).strip()
                if cc in candidates:
                    return c
            raise ValueError(f"필수 컬럼 누락: {candidates}")
        def find_col_optional(candidates: list[str]) -> Optional[str]:
            for c in df.columns:
                cc = str(c).strip()
                if cc in candidates:
                    return c
            return None

        col_empnum = find_col(['사번(필수)','사번','EmpNum','emp_num'])
        col_acc = find_col(['계정 ID(필수)','계정 ID','ID','아이디','account_id'])
        col_name = find_col(['직원명(필수)','이름','성명','name'])
        col_role = find_col(['직무(필수)','직무','role'])
        col_exp = find_col(['경력(필수)','경력','experience'])
        col_head = find_col(['수간호사여부(필수)','수간호사여부','is_head_nurse'])
        col_join = find_col_optional(['입사일(선택)','입사일','joining_date'])
        # col_resi = find_col_optional(['적용해제일(선택)','적용해제일','퇴사일','resignation_date'])
        col_birth = find_col(['생년월일(필수)', '생년월일', 'birth_date'])
        col_phone = find_col(['연락처(필수)', '연락처', 'phone_number'])
        col_gender = find_col(['성별(필수)', '성별', 'gender'])
        col_email = find_col_optional(['이메일(선택)', '이메일', 'email'])
        
        office_id = user.office_id
        rows_allowed = msdb_manager.fetch_all(Member.member_accounts_by_office(), params=(str(office_id),))
        allowed: dict[str, tuple[str, str | None]] = {}
        for r in rows_allowed or []:
            acc = str(r.get('account_id', '')).strip()
            nm = str(r.get('name', '')).strip()
            auth = r.get('EmpAuthGbn')
            nurse_id = str(r.get('nurse_id', uuid.uuid4())).strip()
            if acc:
                allowed[acc] = (nm, auth, nurse_id)
        normalized: list[dict] = []
        errors: list[dict] = []
        head_count = 0
        acc_in_file: set[str] = set()

        def parse_dt(v):
            try:
                if pd.isna(v) or str(v).strip() == '':
                    return None
                return pd.to_datetime(v, errors='coerce').to_pydatetime()
            except Exception:
                return None

        for i, row in df.iterrows():
            ridx = int(i) + 2
            row_errs: list[str] = []
            emp_num_val = row.get(col_empnum)
            emp_num = '' if pd.isna(emp_num_val) else str(emp_num_val).strip()
            account_id = str(row.get(col_acc, '')).strip()
            name = str(row.get(col_name, '')).strip()
            role_val = row.get(col_role)
            role = 'RN' if pd.isna(role_val) or not str(role_val).strip() else str(role_val).strip()
            birth_date = row.get(col_birth)
            phone_num = row.get(col_phone)
            gender = row.get(col_gender)
            email_val = str(row.get(col_email, '')).strip() if col_email else None
            email_val = email_val if email_val else None
            # experience: 비어있으면 None 허용, 값이 있으면 숫자만 허용
            exp_val = None
            raw_exp_val = row.get(col_exp, 1)
            # print('raw_exp_val', raw_exp_val)
            import math
            # NaN/None/빈문자/문자 'nan' 전부 필터링
            if raw_exp_val not in ['', None] and not (isinstance(raw_exp_val, float) and math.isnan(raw_exp_val)) and str(raw_exp_val).lower() != 'nan':
                try:
                    exp_val = int(float(str(raw_exp_val).strip()))
                except Exception:
                    # print('raw_exp_val', raw_exp_val)
                    row_errs.append("경력은 숫자여야 합니다. 예: 1, 3, 10")
            head_raw = str(row.get(col_head, '')).strip().upper()
            is_head = True if head_raw in ['Y','YES','1','TRUE','T'] else False
            if is_head:
                head_count += 1
            joining_raw = row.get(col_join) if col_join else None
            # resignation_raw = row.get(col_resi) if col_resi else None
            # 날짜: None 허용, 숫자 허용, 문자열인 경우에만 YYYY-MM-DD 형식 검증
            joining_dt = parse_dt(joining_raw) if col_join else None
            if isinstance(joining_raw, str) and joining_raw.strip() and not re.match(r'^\d{4}-\d{2}-\d{2}$', joining_raw.strip()):
                row_errs.append("입사일 형식이 올바르지 않습니다. 예: 2025-10-10")
            # resignation_dt = parse_dt(resignation_raw) if col_resi else None
            # if isinstance(resignation_raw, str) and resignation_raw.strip() and not re.match(r'^\d{4}-\d{2}-\d{2}$', resignation_raw.strip()):
            #     row_errs.append("적용해제일 형식이 올바르지 않습니다. 예: 2025-10-10")
            if not account_id:
                row_errs.append("계정 ID 누락")
            elif not re.match(r'^\S{1,50}$', account_id):
                row_errs.append("계정 ID 형식 오류: 공백 제외 최대 50자까지 허용됩니다.")
            # elif account_id in acc_in_file:
                # existing = db.query(NurseModel.account_id).filter(NurseModel.account_id.in_(list(acc_in_file))).all()
                # if existing:
                #     row_errs.append('이미 존재하는 계정 ID')
            elif account_id in acc_in_file:
                row_errs.append("엑셀 내 중복 계정 ID")
            elif account_id not in allowed:
                row_errs.append(f"계정이 등록되지 않았습니다.")
            if not name:
                # 원장에서 이름 보강
                name = (allowed.get(account_id, ("", None))[0] if account_id in allowed else "")
                if not name:
                    row_errs.append("직원명 누락")
            if not role:
                row_errs.append("직무 누락")
            else:
                acc_in_file.add(account_id)
            
            def is_invalid_value(v):
                """이름 등 필드가 비어있거나 nan인지 검사. float nan / pd.NA / 문자열 'nan' 포함."""
                if v is None:
                    return True
                if isinstance(v, float) and math.isnan(v):
                    return True
                try:
                    if pd.isna(v):
                        return True
                except Exception:
                    pass
                if isinstance(v, str):
                    return v.strip().lower() in ("", "nan", "none", "null")
                return False
            
            if pd.isna(emp_num_val):
                emp_num = '-'
            elif isinstance(emp_num_val, float) and emp_num_val.is_integer():
                emp_num = str(int(emp_num_val))
            else:
                emp_num = str(emp_num_val).strip()
            if is_invalid_value(name):
                row_errs.append("등록된 이름이 없습니다.")

            if account_id in allowed:
                normalized.append({
                    'row': ridx,
                    'emp_num': emp_num or None,
                    'account_id': account_id,
                    'name': name,
                    'role': role,
                    'experience': exp_val,
                    'is_head_nurse': is_head,
                    'joining_date': joining_dt.isoformat() if joining_dt else None,
                    # 'resignation_date': resignation_dt.isoformat() if resignation_dt else None,
                    'nurse_id': allowed[account_id][2],
                    'birth_date': birth_date,
                    'phone_number': phone_num,
                    'allowed_shifts': [],
                    'gender': gender,
                    'email': email_val,
                })
        
            if row_errs:
                errors.append({'row': ridx, 'reason': ' | '.join(row_errs)})

        existing_head_nurses = db.query(NurseModel).filter(
            NurseModel.office_id == user.office_id,
            NurseModel.group_id == group_id,
            NurseModel.is_head_nurse == 1
        ).count()
        # 전역 검증: 수간호사 최소 1명
        # if head_count == 0:
        if head_count == 0 and existing_head_nurses == 0:
            errors.append({'row': 0, 'reason': '수간호사는 최소 1명 이상이어야 합니다.'})
        # 전역 검증: DB 중복 계정

        return {
            'success': 0 if errors else len(normalized),
            'errors': errors,
            'rows': normalized,
            'summary': {
                'total': len(normalized),
                'head_nurses': head_count,
                'error_count': len(errors),
            }
        }
    except Exception as e:
        print('error', e)
        return {"success": 0, "errors": [{"row": 0, "reason": str(e)}], "rows": [], 'summary': {'total': 0, 'head_nurses': 0, 'error_count': 1}}


#

# def upload2_confirm(rows: List[Dict[str, Any]], user: UserSchema, db: Session, target_group_id: str) -> Dict[str, Any]:
#     """업로드2: 검증된 행을 저장한다. 오류 포함 행은 건너뜀."""
    

#     try:
#         if not target_group_id:
#             print("[ERROR] target_group_id가 없습니다!")
#             return {"success": 0, "errors": [{"row": 0, "reason": "group_id가 필요합니다."}]}

#         saved = 0
#         updated = 0
#         errors = [] # 추가

#         for idx, item in enumerate(rows, 1):
#             print(f"\n[행 {idx:2d}] 처리 시작 ───────────────────────────────────────")
            
#             account_id = item.get('account_id')
#             name = item.get('name')
#             role = item.get('role', 'RN')
#             exp_val = item.get('experience', 1)
#             nurse_id_raw = item.get('nurse_id')
#             nurse_id = str(nurse_id_raw).strip() if nurse_id_raw else None
#             is_head = bool(item.get('is_head_nurse', False))
#             emp_num = item.get('emp_num', '')
#             jd = item.get('joining_date')
#             birth_dt = item.get('birth_date')
#             phone_number = item.get('phone_number')
#             gender = item.get('gender')
#             allowed_shifts = item.get('allowed_shifts', [])
#             work_shifts_val = item.get('work_shifts', [])

#             print(f"   • account_id     : {account_id}")
#             print(f"   • nurse_id (원본) : {nurse_id_raw}")
#             print(f"   • nurse_id (처리후): {nurse_id}")
#             print(f"   • name           : {name}")
#             print(f"   • is_head_nurse  : {is_head}")
#             print(f"   • experience     : {exp_val}")
#             print(f"   • joining_date   : {jd}")
#             print(f"   • birth_date     : {birth_dt}")
#             print(f"   • phone_number   : {phone_number}")
#             print(f"   • gender         : {gender}")            # nurse_id 안전 처리
#             if not nurse_id or len(nurse_id) < 8:
#                 nurse_id = str(uuid.uuid4())
#                 print(f"   → nurse_id 이상 → 새 UUID 생성: {nurse_id}")

#             existing = db.query(NurseModel).filter(NurseModel.account_id == account_id).first()

#             if existing:
#                 print(f"   → 기존 레코드 발견! nurse_id={existing.nurse_id}, name={existing.name}")
#                 print(f"      현재 DB 값 - experience={existing.experience}, is_head_nurse={existing.is_head_nurse}")

#                 if name and existing.name != name:
#                     existing.name = name
#                     print("      → 이름 업데이트")

#                 existing.emp_num = emp_num if emp_num is not None else ''
#                 existing.role = role if role is not None else ''
                
#                 if isinstance(exp_val, int):
#                     existing.experience = exp_val
#                     print(f"      → experience 업데이트: {exp_val}")

#                 existing.is_head_nurse = is_head

#                 try:
#                     existing.office_id = user.office_id
#                     print("      → office_id 강제 업데이트")
#                 except Exception:
#                     print("      → office_id 업데이트 실패 (무시)")

#                 if jd:
#                     try:
#                         joining_dt = pd.to_datetime(jd).to_pydatetime()
#                         existing.joining_date = joining_dt
#                         print(f"      → joining_date 업데이트: {joining_dt}")
#                     except:
#                         print("      → joining_date 변환 실패")

#                 existing.birth_date = birth_dt
#                 existing.phone_number = phone_number
#                 existing.gender = gender
#                 existing.allowed_shifts = allowed_shifts
#                 existing.work_shifts = work_shifts_val
                
#                 updated += 1
#                 print(f"   → 업데이트 완료 (현재 updated 누적: {updated})")
#                 continue

#             print("   → 신규 등록 시작")
#             try:
#                 seq_next = get_next_sequence(target_group_id, 1, db)
#                 print(f"      • 다음 sequence 값: {seq_next}")

#                 new_nurse = NurseModel(
#                     nurse_id=nurse_id,
#                     group_id=target_group_id,
#                     office_id=user.office_id,
#                     emp_num=emp_num if emp_num is not None else '',
#                     account_id=account_id,
#                     name=name or account_id,
#                     experience=exp_val if isinstance(exp_val, int) else 1,
#                     role=role if role is not None else '',
#                     level_='일반',
#                     is_head_nurse=is_head,
#                     allowed_shifts=allowed_shifts,
#                     personal_off_adjustment=0,
#                     preceptor_id=None,
#                     joining_date=pd.to_datetime(jd).to_pydatetime() if jd else None,
#                     sequence=seq_next,
#                     active=1,
#                     birth_date=birth_dt,
#                     phone_number=phone_number,
#                     gender=gender,
#                     work_shifts=work_shifts_val,
#                 )

#                 print("      • new_nurse 객체 생성 완료")
#                 import pprint
#                 print("      • new_nurse 내용:")
#                 pprint.pprint(new_nurse.__dict__)

#                 db.add(new_nurse)
#                 print(f"      • db.add() 완료 (nurse_id={nurse_id})")
#                 saved += 1
#                 print(f"   → 신규 등록 완료 (현재 saved 누적: {saved})")

#             except Exception as inner_e:
#                 print(f"   ★★★ 신규 등록 중 오류 ★★★")
#                 print(f"      • 에러 타입: {type(inner_e).__name__}")
#                 print(f"      • 에러 메시지: {str(inner_e)}")
#                 import traceback
#                 traceback.print_exc()
#                 errors.append({"row": item.get('row', 0), "reason": str(inner_e)})

#         print("\n[upload2_confirm] COMMIT 직전 상태")
#         print(f"  • 최종 saved = {saved}")
#         print(f"  • 최종 updated = {updated}")
#         print(f"  • 누적 에러 개수 = {len(errors)}")

#         db.commit()
#         print("[upload2_confirm] ★★★ COMMIT 성공 ★★★")
#         print("====================================================")

#         return {"success": saved + updated, "saved": saved, "updated": updated, "errors": errors}

#     except Exception as e:
#         print("[upload2_confirm] ★★★ 전체 예외 발생 ★★★")
#         print(f"  • 에러 타입: {type(e).__name__}")
#         print(f"  • 에러 메시지: {str(e)}")
#         import traceback
#         traceback.print_exc()
#         db.rollback()
#         print("[upload2_confirm] ROLLBACK 완료")
#         print("====================================================")
        
#         return {"success": 0, "errors": [{"row": 0, "reason": f"저장 실패: {str(e)}"}]}


def get_excel_column_by_field(field: str, mapping: Dict[str, str]) -> str:
    """필드명으로 엑셀 컬럼명 찾기"""
    for excel_col, db_field in mapping.items():
        if db_field == field:
            return excel_col
    raise ValueError(f"필드 {field}에 해당하는 엑셀 컬럼을 찾을 수 없습니다.")


def parse_boolean(value: Any) -> bool:
    """다양한 형태의 불린 값 파싱"""
    if pd.isna(value):
        return False
    
    str_value = str(value).strip().upper()
    return str_value in ['Y', 'YES', 'TRUE', '1', 'T', '참', '예']


def validate_single_row(group_name: str, nurse_data: Dict[str, Any], user: UserSchema, db: Session) -> Dict[str, Any]:
    """개별 행 데이터 검증"""
    
    errors = []
    warnings = []
    is_overwrite = False
    
    # # 병동명 검증
    # if not nurse_data.get('group_name'):
    #     errors.append("병동명은 필수입니다.")
    # elif not nurse_data.get('group_id'):
    #     errors.append("유효하지 않은 병동명입니다.")
    
    # 필수 필드 검증
    if not nurse_data.get('account_id'):
        errors.append("계정 ID는 필수입니다.")
    elif not re.match(r'^\S{1,50}$', nurse_data['account_id']):
        errors.append("계정 ID 형식 오류: 공백 제외 최대 50자까지 허용됩니다.")
    
    if not nurse_data.get('name'):
        errors.append("이름은 필수입니다.")
    
    if nurse_data.get('experience', 0) < 1:
        errors.append("경력은 1년 이상이어야 합니다.")
    
    if not nurse_data.get('role'):
        errors.append("직군은 필수입니다.")
    
    if not nurse_data.get('level_'):
        errors.append("직책은 필수입니다.")
    
    # 중복 검사
    if nurse_data.get('group_id'):
        existing_nurse = db.query(NurseModel).filter(
            NurseModel.nurse_id == nurse_data['nurse_id']
        ).first()
        
        if existing_nurse:
            is_overwrite = True
            warnings.append(f"기존 간호사 '{existing_nurse.name}' 데이터를 덮어씁니다.")
        
        # 계정 ID 중복 검사 (다른 nurse_id와)
        existing_account = db.query(NurseModel).filter(
            NurseModel.account_id == nurse_data['account_id'],
            NurseModel.nurse_id != nurse_data['nurse_id']
        ).first()
        
        if existing_account:
            errors.append(f"계정 ID '{nurse_data['account_id']}'는 이미 사용 중입니다.")
    
    return {
        'is_valid': len(errors) == 0,
        'is_overwrite': is_overwrite,
        'errors': errors,
        'warnings': warnings,
        'nurse_data': nurse_data,
        'group_name': group_name
    }


def validate_excel_data(data: List[Dict[str, Any]], user: UserSchema, db: Session) -> Dict[str, Any]:
    """엑셀 데이터 전체 유효성 검증"""
    
    validation_results = []
    
    for nurse_data in data:
        if nurse_data is None:
            validation_results.append({'is_valid': False, 'errors': ['데이터 파싱 오류']})
            continue
            
        result = validate_single_row(nurse_data, user, db)
        validation_results.append(result)

    
    # 수간호사 최소 1명 검증
    head_nurses = [
        result for result in validation_results 
        if result.get('is_valid') and result.get('nurse_data', {}).get('is_head_nurse')
    ]
    
    if len(head_nurses) == 0:
        # 기존 수간호사가 있는지 확인
        existing_head_nurses = db.query(NurseModel).filter(
            NurseModel.group_id == user.group_id,
            NurseModel.is_head_nurse == True
        ).count()
        
        if existing_head_nurses == 0:
            return {
                'success': False,
                'error': '최소 1명의 수간호사가 필요합니다.',
                'validation_results': validation_results
            }
    
    valid_count = sum(1 for result in validation_results if result.get('is_valid', False))
    error_count = len(validation_results) - valid_count
    overwrite_count = sum(1 for result in validation_results if result.get('is_overwrite', False))
    print('error_count', error_count)
    return {
        'success': True,
        'validation_results': validation_results,
        'summary': {
            'total': len(validation_results),
            'valid': valid_count,
            'error': error_count,
            'overwrite': overwrite_count
        }
    }


def save_excel_data(data: List[Dict[str, Any]], user: UserSchema, db: Session) -> Dict[str, Any]:
    """검증된 데이터 DB 저장"""
    
    try:
        saved_count = 0
        updated_count = 0
        for nurse_data in data:
            if not nurse_data:
                continue
                
            # 기존 데이터 확인
            existing_nurse = db.query(NurseModel).filter(
                NurseModel.nurse_id == nurse_data['nurse_id']
            ).first()
            
            if existing_nurse:
                # 업데이트
                for key, value in nurse_data.items():
                    if hasattr(existing_nurse, key):
                        setattr(existing_nurse, key, value)
                existing_nurse.updated_at = datetime.now()
                updated_count += 1
            else:
                # 신규 생성
                new_nurse = NurseModel(**nurse_data)
                db.add(new_nurse)
                saved_count += 1
        
        db.commit()
        
        return {
            'success': True,
            'message': f'저장 완료: 신규 {saved_count}건, 업데이트 {updated_count}건',
            'saved_count': saved_count,
            'updated_count': updated_count
        }
        
    except Exception as e:
        db.rollback()
        return {
            'success': False,
            'error': f'저장 실패: {str(e)}'
        } 


def create_groups_and_save_data(data: List[Dict[str, Any]], new_groups_to_create: List[str], user: UserSchema, db: Session) -> Dict[str, Any]:
    """새 그룹 생성 후 데이터 저장"""
    
    try:
        # 새 그룹 생성
        created_groups = {}
        for group_name in new_groups_to_create:
            # 새 그룹 ID 생성
            new_group_id = generate_new_group_id(user.office_id, db)
            # print('new_group_id', new_group_id)
            # 그룹 생성
            create_new_group(group_name, new_group_id, user, db)
            created_groups[group_name] = new_group_id

        # 데이터의 group_id 업데이트 (이미 생성된 그룹 ID 사용)
        for nurse_data in data:
            if nurse_data:
                group_name = nurse_data.get('group_name')
                if group_name in created_groups:
                    nurse_data['group_id'] = created_groups[group_name]
        
        # 데이터 저장
        result = save_excel_data(data, user, db)
        if result['success']:
            result['created_groups'] = created_groups
            result['message'] = f"{result['message']} (새 병동 {len(created_groups)}개 생성)"
        
        return result
        
    except Exception as e:
        db.rollback()
        return {
            'success': False,
            'error': f'그룹 생성 및 저장 실패: {str(e)}'
        } 


def _merge_team_cell(ws, col, first_row, last_row, label, center, border_all, gray_fill):
    """팀별보기 전용: [first_row, last_row] 의 팀 컬럼(col)을 세로 병합하고 팀명 1개를
    가운데 정렬로 기입한다. 병합 범위 전 행에 테두리·배경을 둔다."""
    if last_row > first_row:
        ws.merge_cells(start_row=first_row, start_column=col, end_row=last_row, end_column=col)
    cell = ws.cell(row=first_row, column=col, value=label)
    cell.alignment = center
    cell.fill = gray_fill
    cell.border = border_all
    for r in range(first_row, last_row + 1):
        cc = ws.cell(row=r, column=col)
        cc.border = border_all
        cc.fill = gray_fill


def _sort_team_ids_by_name(team_name_map: dict) -> list:
    """팀 id 들을 근무표 만들기 화면(team-sort-util.sortTeamGroupsByTeamName)과 동일
    순서로 정렬한다. 1순위 그룹: 한글(0) < 영문(1) < 숫자(2) < 기타(3). 2순위: 자연
    정렬(숫자 런은 int 로 비교)+소문자. 3순위: team_id.
    (export 엔 활성 실팀만 오므로 미배정/임시팀 분기는 불필요.)
    """
    import re as _re

    def _group_rank(name: str) -> int:
        first = (name or "").strip()[:1]
        if not first:
            return 3
        if "가" <= first <= "힣":
            return 0
        if first.isascii() and first.isalpha():
            return 1
        if first.isdigit():
            return 2
        return 3

    def _natural_key(name: str) -> list:
        # Intl.Collator({numeric:true}) 근사: 숫자 런은 (0,int), 그 외는 (1,소문자).
        # 토큰 타입을 앞에 둬 int/str 직접 비교(TypeError)를 원천 차단.
        out = []
        for tok in _re.findall(r"\d+|\D+", name or ""):
            out.append((0, int(tok)) if tok.isdigit() else (1, tok.casefold()))
        return out

    def _key(tid):
        name = team_name_map.get(tid) or ""
        return (_group_rank(name), _natural_key(name), tid)

    return sorted(team_name_map.keys(), key=_key)


#: 요일 라벨. `calendar.weekday()` 가 월=0 이므로 그 순서다.
_WD = ["월", "화", "수", "목", "금", "토", "일"]

#: 헤더 글자색 — 병원이 준 시트의 실측값을 그대로 쓴다(2026-10 시트).
#:   토 파랑 · 일 빨강 · 공휴일 빨강. **공휴일이 토요일을 이긴다**(10/3 개천절이 토인데 빨강).
_C_SAT = "FF0000FF"
_C_SUN_HOL = "FFFF0000"


def _kr_holidays_in_month(year: int, month: int) -> set:
    """그 달의 한국 공휴일(대체공휴일 포함). 조회 실패 시 **빈 집합**.

    ★ `holiday_pack` 을 쓰지 않는다 — 그쪽은 import 실패 시 `ImportError` 를 그대로
      raise 해서 모듈이 통째로 깨지고, 쓰지도 않는 `langchain_core.tools` 를 끌어온다.
      엑셀 다운로드가 패키지 문제 하나로 죽으면 안 되므로,
      `roster_create_service._kr_holidays_in_month` 와 같은 방어형으로 간다.
    ★ 표시용이라 그룹 설정(`fixed_holiday_off_yn`)과 무관하게 **달력 기준**으로 칠한다.
      그 설정은 고정근무자 배정용이지 표시 규칙이 아니다.
    """
    try:
        import holidays as _h

        return {d.day for d in _h.KR(years=[year]) if d.year == year and d.month == month}
    except Exception as exc:  # noqa: BLE001
        print(f"[Excel] 공휴일 조회 실패 — 주말만 칠한다: {exc}")
        return set()


def _day_font_color(year: int, month: int, day: int, holidays: set):
    """일자/요일 헤더의 글자색. 해당 없으면 None(기본 검정)."""
    import calendar as _cal

    if day in holidays:
        return _C_SUN_HOL
    wd = _cal.weekday(year, month, day)
    if wd == 6:
        return _C_SUN_HOL
    if wd == 5:
        return _C_SAT
    return None


def _norm_hex(color) -> Optional[str]:
    """`shifts.color` → openpyxl 이 받는 `RRGGBB`. 못 읽으면 None.

    ★ openpyxl 은 `#` 를 붙이면 값을 버리거나 예외를 낸다. 반드시 떼야 한다.
    ★ 값 검증이 어디에도 없어 `#RGB` 축약형과 빈 문자열이 실제로 들어온다.
      **빈 값을 흰색으로 떨어뜨리면 안 된다** — 흰 배경에 흰 글자가 되어 완전히 안 보인다.
      읽을 수 없으면 None 을 돌려 호출부가 색을 아예 안 칠하게 한다.
    """
    s = str(color or "").strip().lstrip("#")
    if len(s) == 3 and all(ch in "0123456789abcdefABCDEF" for ch in s):
        s = "".join(ch * 2 for ch in s)          # #abc → aabbcc
    if len(s) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in s):
        return None
    return s.upper()


#: 이 명도 이상이면 **검은 글자**, 아니면 흰 글자. YIQ 기준.
#:
#: ★★ 프론트(`getReadableShiftCodeTextColor`)는 **225** 를 쓰는데, 엑셀에는 그대로
#:   못 쓴다. 실제 근무코드 색이 전부 **149~212** 구간이라 225 로는 **18개 코드가 전부
#:   흰 글자**가 된다(실측). `#FFA0D2`(OFF, 명도 194)·`#C8E0B8`(보수, 212) 같은 파스텔에
#:   흰 글자면 종이로 뽑았을 때 사실상 안 보인다.
#:   150 이면 17/18 이 검은 글자가 되고, 어두운 `DA`(#7E9BB5, 149.3) 만 흰 글자로 남는다.
#: ★ 화면과 판정이 갈리는 것은 **의도한 것**이다 — 엑셀은 작은 셀에 인쇄되는 매체라
#:   가독성을 우선한다. 화면과 맞추고 싶으면 이 값만 225 로 되돌리면 된다.
_YIQ_DARK_TEXT_THRESHOLD = 150


def _readable_text_color(bg_hex: str) -> str:
    """배경 위에 얹을 글자색. 계산식은 프론트와 같고 **임계만** 다르다(위 상수 참조)."""
    return "FF101828" if _yiq(bg_hex) >= _YIQ_DARK_TEXT_THRESHOLD else "FFFFFFFF"


def _yiq(bg_hex: str) -> float:
    """`RRGGBB` 의 YIQ 명도. `_norm_hex` 를 통과한 값만 넣는다."""
    r, g, b = int(bg_hex[0:2], 16), int(bg_hex[2:4], 16), int(bg_hex[4:6], 16)
    return (r * 299 + g * 587 + b * 114) / 1000


#: 원티드 표시색 — 가르는 축은 **누가 그 칸을 정했는가** 다.
#:   · 간호사가 제출한 원티드가 반영된 칸       → 빨강
#:   · 수간호사가 확정 원티드에 덮어쓰거나 넣은 칸 → 파랑
#: 원티드와 무관한 칸은 색을 주지 않는다(검정).
#: ★ 판별은 제출 테이블(`nurse_shift_requests`)과 **직접 대조**한다. `source_type` 은
#:   저장 시점 판정이라 수간호사가 간호사 제출 전에 넣어 두면 'added' 로 굳어 버린다.
#: ★ 배정 영역은 대표 코드에만 배경을 칠하므로, 무배경 흰 셀에서도 읽히는 진한 색을 쓴다.
_EMPTY_SET: frozenset = frozenset()
_EMPTY_MAP: dict = {}
#: 팀별보기에서 파트장 행의 그룹 키. `team_of` 의 정상값(int·None)과 겹치지 않아야
#:   병합이 파트장 구간을 팀 블록과 섞지 않는다.
_HN_TEAM_KEY = "__hn__"


def _as_date(v):
    """`date`/`datetime`/None 을 `date`(또는 None)로 맞춘다.

    ★ `datetime` 은 `date` 의 **서브클래스**라 `isinstance` 순서를 뒤집으면 안 된다.
    """
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v
_C_WANTED = "FFFF0000"            # 확정 원티드로 들어간 칸 — 빨강
_C_SPECIAL = "FF0000FF"           # 기타 특수코드(보수교육·공가·연차 등) — 파랑


def export_schedule_excel_bytes(
    schedule_id: str, current_user, db, target_group_id: str, group_by_team: bool = False
) -> tuple[bytes, bool]:
    """지정된 schedule_id의 근무표를 엑셀(xlsx) 바이트로 생성하여 (bytes, team_view) 로 반환.

    group_by_team=True 이고 실제 팀 배정이 있으면 '팀별보기' 레이아웃으로 그린다:
    기존 레이아웃과 동일하되 맨 앞(컬럼1)에 팀 세로병합 컬럼 1개를 추가한다.

    팀 구분은 **근무표 만들기 화면의 '팀별보기'(filterTeam)와 완전 동일한 규칙**으로 한다:
      teamId = is_night_dedicated ? None : (as_of_team ?? nurses.team_id 캐시)
    - 소속·N전담·시점팀(as_of_team)은 화면과 같은 group_members_in_month 를 재사용한다.
      → N전담은 팀 로테이션 비참여 → '미등록'.
    - 활성 팀(Team.active==1, /teams 와 동일 소스)에 없는 team_id 는 '미등록'으로 둔다.
    - 팀 블록 순서는 화면(sortTeamGroupsByTeamName)과 동일한 팀명 기준 + 미등록 맨 끝.
    같은 팀 간호사 행들의 컬럼1을 세로 병합해 팀명 1개만 표시한다(팀 내부는 기존 순서 유지).

    반환 team_view 는 실제로 팀별보기 레이아웃이 적용됐는지 여부(파일명/헤더 판정용).
    팀 배정이 전무(전원 미등록)하거나 group_by_team=False 이면 기존 레이아웃과
    완전히 동일하게 그리고 team_view=False 를 반환한다(회귀 0).
    """
    from io import BytesIO
    from datetime import date, timedelta
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    from db.models import Schedule, ScheduleEntry, Nurse, Shift, RosterConfig, ShiftManage
    import calendar

    # ───────── 1) 데이터 로드 ─────────
    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.dropped == False
    ).first()
    if not schedule:
        raise ValueError("스케줄을 찾을 수 없습니다.")

    year, month = schedule.year, schedule.month
    days_in_month = calendar.monthrange(year, month)[1]

    nurses = db.query(
        Nurse.nurse_id, Nurse.name, Nurse.experience, Nurse.sequence, Nurse.role,
        Nurse.hn_auth,
    ).filter(
        Nurse.group_id == target_group_id
    ).order_by(Nurse.sequence.asc(), Nurse.nurse_id.asc()).all()

    # ───────── 전입자(인바운드) union — 팀별보기·기본 모두 동일하게 적용 ─────────
    # 이 schedule의 ScheduleEntry에는 있으나 현재 group 멤버가 아닌 간호사(이동 발효 전
    # nurses.group_id 미반영 등)를 추가한다. 화면(get_roster_by_schedule_id)과 동일 기준.
    _home_nurse_ids = {n.nurse_id for n in nurses}
    _entry_nurse_ids = {
        row.nurse_id
        for row in db.query(ScheduleEntry.nurse_id)
        .filter(ScheduleEntry.schedule_id == schedule_id)
        .distinct()
        .all()
    }
    _inbound_ids = _entry_nurse_ids - _home_nurse_ids
    if _inbound_ids:
        inbound_nurses = db.query(
            Nurse.nurse_id, Nurse.name, Nurse.experience, Nurse.sequence, Nurse.role,
            Nurse.hn_auth,
        ).filter(
            Nurse.nurse_id.in_(_inbound_ids)
        ).order_by(Nurse.sequence.asc(), Nurse.nurse_id.asc()).all()
        nurses = list(nurses) + list(inbound_nurses)

    # ───────── 팀별보기 분기 결정 ─────────
    # 근무표 만들기 화면 '팀별보기'(filterTeam)와 **완전 동일 규칙**으로 팀을 구분한다:
    #   teamId = is_night_dedicated ? None : (as_of_team ?? nurses.team_id 캐시)
    #   gate: teamId 가 활성 팀(Team.active==1) 목록에 없으면 미등록(None).
    # 소속·N전담·시점팀(as_of_team)은 화면과 같은 group_members_in_month 를 그대로 재사용한다.
    # 전원 미등록(팀 배정 전무)이거나 group_by_team=False 면 기존 레이아웃으로 폴백.
    team_view = False
    team_of: dict[str, Optional[int]] = {}     # nurse_id(str) -> gated team_id(int)|None
    nurse_team_label: dict[str, str] = {}      # nurse_id(str) -> 팀명/"미등록"
    if group_by_team:
        from services.assignment_service import group_members_in_month
        from services.team_period import _coerce_team_int
        from db.models import Team

        # (1) 화면 memberStatusMap 동치: as_of_team(시점 팀, N전담=None) + is_night_dedicated.
        _member_map = {
            m["nurse_id"]: m
            for m in group_members_in_month(db, target_group_id, year, month)["members"]
        }
        # (2) knownTeamIds·팀명 = 활성 팀(useTeamsQuery/list_teams_with_members 와 동일 소스).
        _team_name_map = {
            t.team_id: t.team_name
            for t in db.query(Team.team_id, Team.team_name).filter(
                Team.office_id == current_user.office_id,
                Team.group_id == target_group_id,
                Team.active == 1,
            ).all()
        }
        _known_team_ids = set(_team_name_map.keys())
        # (3) as_of_team 이 없을 때의 캐시 폴백(nurses.team_id) — filterTeam 의 `?? nurse.team_id`.
        _cache_team = {
            str(nid): _coerce_team_int(tid)
            for nid, tid in db.query(Nurse.nurse_id, Nurse.team_id).filter(
                Nurse.nurse_id.in_([str(n.nurse_id) for n in nurses])
            ).all()
        }

        for n in nurses:
            nid = str(n.nurse_id)
            m = _member_map.get(nid)
            # N전담도 직접 배정한 팀(as_of_team=period)을 그대로 표시 — 직접 저장한 팀이
            #   '미지정'으로 가려지던 문제 해소. 프론트도 동일하게
            #   `is_night_dedicated ? None : as_of_team` → `as_of_team` 으로 맞춰야 한다.
            as_of = m.get("as_of_team") if m else None
            tid = as_of if as_of is not None else _cache_team.get(nid)
            # 활성 팀 메타에 없으면(미존재/비활성/타그룹 캐시) 미등록으로 안전 배치(누락 방지).
            team_of[nid] = tid if (tid is not None and tid in _known_team_ids) else None

        if any(tid is not None for tid in team_of.values()):
            team_view = True
            # 팀 블록 순서 = 화면 sortTeamGroupsByTeamName(팀명 기준) + 미등록 맨 끝.
            #   stable sort → 같은 팀(또는 미등록) 내부는 기존 sequence 순서 유지.
            _ordered = _sort_team_ids_by_name(_team_name_map)
            _rank = {tid: i for i, tid in enumerate(_ordered)}
            _unassigned_rank = len(_ordered)
            nurses = sorted(
                nurses,
                key=lambda n: _rank.get(team_of.get(str(n.nurse_id)), _unassigned_rank),
            )
            for n in nurses:
                tid = team_of.get(str(n.nurse_id))
                nurse_team_label[str(n.nurse_id)] = (
                    "미등록" if tid is None else (_team_name_map.get(tid) or f"팀 {tid}")
                )

    # ───────── 파트장(hn_auth='HN')은 항상 맨 위 ─────────
    #   기본 보기에서는 파트장의 `sequence` 가 1 이라 대개 이미 맨 위지만, **팀별보기는
    #   팀 순서로 다시 정렬**하므로 팀이 없는 파트장이 '미등록' 블록과 함께 맨 끝으로
    #   밀린다(실측: 성남 중환자실-RN 송순진 `team_id=None`). 두 레이아웃을 같게 만들기
    #   위해 **모든 정렬이 끝난 뒤** 한 번 더 끌어올린다.
    #   ★ 안정 정렬이라 파트장끼리·나머지끼리의 기존 순서(팀 순서·sequence)는 그대로다.
    #   ★ 판정은 `hn_auth` 한 축이다 — `is_head_nurse` 는 별개 축이고(스위치가 아니라
    #     표시용) 둘이 어긋난 계정이 실재한다([[feedback_hn_auth_orthogonal]]).
    def _is_hn(_n) -> bool:
        return str(getattr(_n, "hn_auth", "") or "").upper() == "HN"

    nurses = sorted(nurses, key=lambda _n: 0 if _is_hn(_n) else 1)

    # ★★ 팀별보기에서는 **팀 컬럼도 같이 옮겨야** 한다. 파트장만 위로 빼고 라벨을 그대로
    #   두면 그 라벨이 **두 블록으로 쪼개진다** — 병합은 연속 구간만 합치기 때문이다.
    #   (실측 운영: 파트장 보유 28개 그룹 중 7곳에서 발현 — 6곳은 '미등록' 이 위아래로
    #    갈리고, 별관1병동은 파트장이 팀2 소속이라 팀2 블록이 둘로 쪼개진다.)
    #   그래서 파트장 행은 **자체 구간**으로 만든다. 팀을 옮기는 게 아니라 '이 줄은
    #   파트장' 이라고 표시하는 것이라 정보가 사라지지 않는다.
    if team_view:
        for _n in nurses:
            if _is_hn(_n):
                _nid = str(_n.nurse_id)
                team_of[_nid] = _HN_TEAM_KEY
                nurse_team_label[_nid] = "파트장"

    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all()

    # alias_map 생성
    alias_map: dict[str, str] = {}
    sm_rows = db.query(ShiftManage).filter(
        ShiftManage.office_id == current_user.office_id,
        ShiftManage.group_id == target_group_id,
    ).all()
    for row in sm_rows:
        if not row.main_code:
            continue
        base = row.main_code.upper()
        alias_map[base] = base
        if row.codes:
            for c in row.codes:
                alias_map[str(c).upper()] = base
    alias_map.setdefault('OFF', 'O')
    alias_map.setdefault('O', 'O')

    def to_base(code: str) -> str:
        if not code:
            return '-'
        u = code.upper()
        if u in alias_map:
            return alias_map[u]
        if u.startswith('D'): return 'D'
        if u.startswith('E'): return 'E'
        if u.startswith('N'): return 'N'
        if u in ('O', 'OFF'): return 'O'
        return u

    # ───────── 2) 실제 사용된 모든 base 코드 수집 ─────────
    used_codes = set()
    for e in entries:
        if e.shift_id:
            base = to_base(e.shift_id)
            if base != '-':
                used_codes.add(base)

    core_codes = ['D', 'E', 'N', 'O']
    extra_codes = sorted(used_codes - set(core_codes))
    tail_labels = core_codes + extra_codes
    summary_cols = len(tail_labels)

    # nurse별 매핑
    by_nurse: dict[str, dict[int, str]] = {}
    for e in entries:
        by_nurse.setdefault(e.nurse_id, {})[e.work_date.day] = e.shift_id

    # ───────── 2-b) 표시용 부가 데이터 ─────────
    #   ★ `shifts` 는 **group_id 로 좁혀** 읽는다. PK·FK 가 없고 `shifts.id` 가 병동 간
    #     중복되므로(id=1874 가 동탄시티 'OFF' 와 시화 '반반반' 양쪽) 전역 조회하면
    #     남의 병동 색을 집는다.
    #   ★ 코드→색은 `sequence`·`id` 순 **대표 1건**만 쓴다. 같은 `(group_id, shift_id)` 가
    #     여러 행인 경우가 실재한다(전사 중복 81개).
    _shift_rows = (
        db.query(Shift)
        .filter(Shift.group_id == target_group_id)
        .order_by(Shift.sequence.asc(), Shift.id.asc())
        .all()
    )
    code_bg: Dict[str, str] = {}
    #: 배정 영역에서 **배경을 칠할** 코드 — 셋이다:
    #:   ① 3교대 대표(`default_shift ∈ D,E,N`)
    #:   ② OFF 교환 대상(`off_swap_target`)
    #:   ③ **연차 계열(`annual_leave_unit IS NOT NULL`)** — `Y`(1.0)·`Y1`/`Y2`(0.5)·
    #:      `Y3`(0.25) 처럼 연차 일수로 환산되는 코드. `off_swap_target` 은 병동마다
    #:      설정이 엇갈려(같은 `Y` 가 어느 병동은 켜져 있고 어느 병동은 아님) 연차 배경이
    #:      들쭉날쭉했는데, 이 컬럼은 연차 정산에 쓰이므로 채워져 있어야만 하는 값이라
    #:      기준으로 삼기에 안정적이다.
    #: 나머지 고정근무·특수코드까지 칠하면 표가 얼룩덜룩해 글자색 표시가 묻힌다.
    #: 범례·집계는 이 제한을 받지 않는다.
    fill_codes: set = set()
    #: **OFF 축** — 원티드가 들어주어진 것을 빨간 글자로 표시할 대상.
    #:   `default_shift == 'O'`(OFF) · `'주'`(주휴) · `off_swap_target`(연차전환대상).
    #:   ★ 주휴를 여기 넣는 건 파랑(특수코드)으로 새는 것을 막기 위함이다. 주휴는
    #:     자동 배정이라 원티드로 들어오지 않으므로 빨강이 켜지지는 않는다.
    off_axis_codes: set = set()
    #: **근무 축** 코드 — 파랑(특수코드) 판정에서 뺀다.
    #:   ★ `M`(MID)까지 넣는다. 배경은 병원 요청대로 D/E/N 만 칠하지만, MID 는 교육·공가
    #:     같은 '특수코드' 가 아니라 엄연한 교대 근무다. 안 빼면 MID 를 쓰는 병동에서
    #:     근무 칸이 통째로 파래진다(실측 dev 61병동-AN `D2` 22칸).
    den_codes: set = set()
    #: `shifts.type == '근무'` 인 코드. 고정근무 코드를 **전역으로** 파랑에서 뺄 때의
    #:   안전판이다 — `fixed_shift` 컬럼은 아무 코드나 담을 수 있어(스키마상 제약 없음),
    #:   누가 실수로 연차 코드를 고정근무로 넣으면 그 코드가 **전 병동에서** 파랑을
    #:   잃는다. 실측 전사 `fixed_shift` 11종·2,044행은 모두 `type='근무'` 라 지금은
    #:   차이가 없지만, 구멍을 열어 둘 이유가 없다.
    work_type_codes: set = set()
    #: 연차 계열(`annual_leave_unit` 이 채워진 코드). 배경을 칠하고 **파랑에서도 뺀다** —
    #:   연차는 교육·공가 같은 '기타 특수코드' 가 아니라 자체 축이고, 원본 근무표 관례상
    #:   원티드로 받은 휴가는 빨간 글자로 나타낸다.
    leave_unit_codes: set = set()
    _seen_code: set = set()
    for _s in _shift_rows:
        _code = str(_s.shift_id or "").strip()
        # ★ 대표 1건 판정은 **별도 집합**으로 한다. 예전엔 `_code in code_bg` 로 걸렀는데
        #   색이 없는 코드는 code_bg 에 안 들어가 뒤쪽 중복행이 계속 덮었다.
        if not _code or _code in _seen_code:
            continue
        _seen_code.add(_code)
        _hex = _norm_hex(_s.color)
        if _hex:
            code_bg[_code] = _hex
        # ★ `default_shift` 는 표기가 하나가 아니다. 생성 경로가 `{"OFF","주"} → "O"` 로
        #   정규화하고 주휴를 `W` 로도 받으므로(`roster_create_service.py:3840-3842`)
        #   여기서도 같은 규약으로 먼저 접는다. 실측상 두 DB 의 `shifts.default_shift`
        #   에는 `O`·`주` 만 있으나, 한쪽만 알아들으면 그 병동에서 OFF 가 조용히
        #   '기타 특수코드'(파랑)로 새고 빨강도 안 켜진다.
        _axis = str(_s.default_shift or "").strip().upper()
        if _axis in ("OFF", "주", "W"):
            _axis = "O"
        _swap = bool(getattr(_s, "off_swap_target", False))
        # ★★ **`shift_gb` 가 제품 자신의 분류축**이다. `default_shift` 만 보면 안 된다 —
        #   전사 1,891행 중 `default_shift` 가 채워진 건 511행뿐인데 `shift_gb` 는
        #   527행이고, 값이 `O`·데이·이브닝·나이트·미드·고정 여섯뿐이라 `type` 과
        #   1:1 로 떨어진다(고정/데이/이브닝/나이트/미드=근무 · O=휴무).
        #   특히 `'고정'` 35행이 파트장·MID고정·탄력근무·일반상근 같은 **고정근무 변형**을
        #   정확히 집는다. 이걸 안 쓰면 `default_shift` 가 빈 근무 변형이 '기타 특수코드'로
        #   새어 파랗게 나온다.
        _gb = str(getattr(_s, "shift_gb", "") or "").strip()
        if str(getattr(_s, "type", "") or "").strip() == "근무":
            work_type_codes.add(_code)
        if _axis in ("D", "E", "N", "M") or _gb in (
            "데이", "이브닝", "나이트", "미드", "고정",
        ):
            den_codes.add(_code)
        if _axis == "O" or _gb == "O" or _swap:
            off_axis_codes.add(_code)
        if getattr(_s, "annual_leave_unit", None) is not None:
            leave_unit_codes.add(_code)
        if _axis in ("D", "E", "N") or _swap or _code in leave_unit_codes:
            fill_codes.add(_code)

    # 고정근무 — **그 사람의 그 칸은 배경을 칠하지 않는다.** 상시 근무라 한 줄이 통째로
    #   같은 색이 되고, 정작 봐야 할 글자색 표시가 묻힌다.
    #   ★★ 축이 **둘**이라 집합도 둘이다. 하나로 묶으면 한쪽이 반드시 틀린다:
    #     · `fixed_by_nurse` (간호사 단위) = **배경 억제**. 코드 단위로 지우면 안 된다 —
    #       고정근무가 `D` 인 사람이 한 명만 있어도 **전 병동의 D 배경이 사라진다**
    #       (실측 성남 중환자실-RN 김은경 `fixed_shift='D'`).
    #     · `fixed_codes` (코드 단위) = **파랑 제외**. 누군가의 고정근무로 쓰이는 코드는
    #       교육·공가 같은 특수코드가 아니라 근무 코드이므로, 다른 사람이 하루 받아도
    #       파랗게 칠하지 않는다.
    #   SSOT 는 `nurse_allowed_shift_period`(as-of 대상월 1일)다. `nurses.fixed_shift`
    #   컬럼은 as-of-TODAY 단방향 캐시라 미래월에 stale 해 쓰지 않는다.
    #   ★ 대상은 `Nurse.group_id` 가 아니라 **이 파일에 실제로 실리는 간호사**다.
    #     전출·인바운드는 현재 소속이 달라도 근무표에 나오므로(위에서 inbound 를 합친다),
    #     소속으로 거르면 그 사람의 고정근무 칸만 처리가 빠진다.
    fixed_codes: set = set()
    #: 간호사 → {일(day): {그 날 유효한 고정근무 코드}}.
    #:   ★★ **날짜까지 봐야 한다.** 달과 겹치기만 하면 그 코드를 한 달 내내 억제하는
    #:     식으로 짜면, 15일부터 고정근무가 시작된 사람의 1~14일 **일반 배정**까지
    #:     배경을 잃는다(월 중 코드가 바뀌면 두 코드 모두 한 달 전체가 눌린다).
    #:     억제 대상은 '그 사람의 그 날 고정근무 칸' 이지 '그 사람의 그 코드' 가 아니다.
    fixed_by_nurse: Dict[str, Dict[int, set]] = {}
    try:
        from sqlalchemy import or_
        from db.models import NurseAllowedShiftPeriod
        #   ★ 월초 시점(as-of)이 아니라 **그 달과 겹치는 구간 전부**를 읽는다. 고정근무가
        #     월 중간부터 시작하는 경우가 있는데, 월초 기준으로만 보면 그 뒤 칸들이
        #     빠진다. `fetch_periods` 와 같은 반열린 구간 규약이다.
        _ms = date(year, month, 1)
        _me = _ms + timedelta(days=days_in_month)
        _nids = [str(_n.nurse_id) for _n in nurses]
        if _nids:
            for _fp in (
                db.query(
                    NurseAllowedShiftPeriod.nurse_id,
                    NurseAllowedShiftPeriod.fixed_shift,
                    NurseAllowedShiftPeriod.valid_from,
                    NurseAllowedShiftPeriod.valid_to,
                )
                .filter(
                    NurseAllowedShiftPeriod.nurse_id.in_(_nids),
                    NurseAllowedShiftPeriod.valid_from < _me,
                    or_(
                        NurseAllowedShiftPeriod.valid_to.is_(None),
                        NurseAllowedShiftPeriod.valid_to > _ms,
                    ),
                )
                .all()
            ):
                _fc2 = str(_fp[1] or "").strip()
                if not _fc2:
                    continue
                # 전역 제외는 **근무 코드일 때만**. 그래야 잘못 설정된 휴가·교육 코드가
                #   남의 칸에서까지 파랑을 잃지 않는다(그 사람 칸은 아래 날짜 맵이 맡는다).
                if _fc2 in work_type_codes:
                    fixed_codes.add(_fc2)
                # 구간을 대상월 안으로 자른다. datetime 으로 올 수 있어 date 로 맞춘다.
                _vf, _vt = _as_date(_fp[2]), _as_date(_fp[3])
                _d0 = max(1, (_vf - _ms).days + 1) if _vf else 1
                _d1 = min(days_in_month, (_vt - _ms).days) if _vt else days_in_month
                if _d1 < _d0:
                    continue
                _slot = fixed_by_nurse.setdefault(str(_fp[0]), {})
                for _dd in range(_d0, _d1 + 1):
                    _slot.setdefault(_dd, set()).add(_fc2)
    except Exception as _fx_exc:
        # 실패하면 고정근무 칸에 배경이 남고, 전용 고정근무 코드는 '기타 특수코드' 로
        #   흘러 파랗게 나온다. 근무표 자체는 내려가야 하므로 표시만 포기하고 사유를 남긴다.
        print(f"[Excel] 고정근무 조회 실패 — 배경 억제·특수코드 제외 모두 생략: {_fx_exc}")

    holidays = _kr_holidays_in_month(year, month)

    # ───────── 2-c) 확정 원티드 ─────────
    #   **확정 원티드(`fixed_wanted_entries`, `is_applied=True`)에 있는 칸 = 빨간 글자.**
    #   ★★ **제출 테이블(`wanted_requests`·`nurse_shift_requests`)과 대조하지 않는다.**
    #     예전엔 '간호사 제출본에도 같은 코드가 있는가' 를 빨강 조건으로 걸었는데,
    #     **운영에서 빨강이 한 칸도 안 나왔다** — 수간호사가 종이 휴가계획서를 받아
    #     확정 원티드에 직접 넣는 운용이 흔해서 제출본이 통째로 비어 있기 때문이다
    #     (실측 2026-09-16 운영 성남시의료원 5개 병동 2026-10: 제출본 **0건** ·
    #      확정원티드 **249건**). 확정에 올라간 것 자체가 '반영된 원티드' 다.
    #   ★★ `year`·`month` 필터를 반드시 건다. `request_id` 가 (간호사,월) 스코프로 1부터
    #     재채번돼, 월 필터 없는 조회가 다른 달 셀을 **일(day)만 떼어** 같은 날짜로 반영한
    #     사고가 있었다(운영 전사 236건). 날짜 범위도 함께 걸어 이중으로 막는다.
    #   ★ 조회가 실패해도 근무표 자체는 내려가야 한다 — 표시만 생략한다.
    wanted_req: Dict[tuple, str] = {}     # (간호사,일) → 확정 원티드 코드
    _wm_start = date(year, month, 1)
    _wm_end = _wm_start + timedelta(days=days_in_month)
    try:
        #    ★ `is_applied == False` 는 **수간호사가 꺼 둔 것**이라 제외한다. 생성·원티드
        #      API 도 전부 `is_applied == True` 만 쓴다
        #      (`wanted_service.py:2841·3639·3980·4070`). 실측으로 성남시의료원에만
        #      꺼진 행이 46건 있다.
        from db.models import FixedWantedEntry
        for _w in (
            db.query(FixedWantedEntry)
            .filter(
                FixedWantedEntry.group_id == target_group_id,
                FixedWantedEntry.year == year,
                FixedWantedEntry.month == month,
                FixedWantedEntry.is_applied == True,      # noqa: E712
                FixedWantedEntry.shift_date >= _wm_start,
                FixedWantedEntry.shift_date < _wm_end,
            )
            .all()
        ):
            _wc = str(_w.shift_id or "").strip()
            if _w.shift_date and _wc:
                wanted_req[(str(_w.nurse_id), _w.shift_date.day)] = _wc
    except Exception as _w_exc:
        print(f"[Excel] 확정 원티드 조회 실패: {_w_exc}")

    # 연차 잔여 — 장부의 `opening` 만 읽는다. '사용'·'당월잔여' 는 **이 근무표 기준**으로
    #   실시간 계산한다(draft 를 받아도 "이대로 마감하면 잔여가 얼마" 가 보여야 한다).
    #: ★★ 장부 **조회**와 사용량 **계산**을 따로 감싼다.
    #:   하나로 묶으면 계산만 실패해도 `leave_opening` 이 비워져 **연차 3열이 통째로
    #:   사라진 채 정상 파일처럼** 내려간다. 사용자는 그게 장애인지 원래 없는 병동인지
    #:   구분할 수 없다 — 조용한 실패가 성공처럼 보이는 것이 가장 나쁘다.
    leave_opening: Dict[str, Any] = {}
    leave_stale = False
    leave_used: Optional[Dict[str, Any]] = {}   # None = 계산 못 함(빈 dict 와 구분한다)
    leave_error: Optional[str] = None
    try:
        from db.models import NurseAnnualLeaveBalance

        for _b in (
            db.query(NurseAnnualLeaveBalance)
            .filter(
                NurseAnnualLeaveBalance.group_id == target_group_id,
                NurseAnnualLeaveBalance.year == year,
                NurseAnnualLeaveBalance.month == month,
            )
            .all()
        ):
            leave_opening[str(_b.nurse_id)] = _b.opening
            # ★★ 장부가 **뒤처졌는지** 여기서 판정한다. 재계산 훅은 fail-open 이라
            #   실패해도 근무표는 커밋된다 — 그러면 틀린 잔여가 맞는 것처럼 보인다.
            #   그 달 마감본이 있는데 `used` 가 비었거나 다른 근무표를 가리키면 stale 이다.
            if schedule.status == "issued":
                if _b.used is None or str(_b.source_schedule_id or "") != str(schedule_id):
                    leave_stale = True
    except Exception as _lv_exc:  # noqa: BLE001
        # 장부 자체를 못 읽었다 → 열을 낼 근거가 없다. 대신 **경고를 남긴다.**
        print(f"[Excel] 연차 장부 조회 실패: {_lv_exc}")
        leave_opening = {}
        leave_error = "연차 장부를 읽지 못했습니다"

    if leave_opening:
        try:
            from services.leave.annual_leave_service import compute_leave_used

            leave_used = compute_leave_used(db, schedule)
        except Exception as _lu_exc:  # noqa: BLE001
            # ★ 계산만 실패했다 → **열은 유지**하고 `사용`·`당월잔여` 만 비운다.
            #   `전월잔여` 는 장부에서 읽은 확정값이라 그대로 보여줄 수 있다.
            print(f"[Excel] 연차 사용량 계산 실패(사용/당월잔여 생략): {_lu_exc}")
            leave_used = None
            leave_error = "연차 사용량을 계산하지 못했습니다"

    #: 연차 열은 **장부가 있는 병동에서만** 낸다(성남시의료원 중환자실 선제공).
    leave_labels = ["전월잔여", "사용", "당월잔여"] if leave_opening else []

    # ───────── 3) 워크북/시트 ─────────
    wb = Workbook()
    ws = wb.active
    ws.title = "근무표"

    # 스타일
    center = Alignment(horizontal="center", vertical="center")
    header_font = Font(bold=True, size=12)
    title_font = Font(bold=True, size=20)
    thin = Side(style="thin", color="000000")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
    gray_fill = PatternFill("solid", fgColor="DEE2E6")
    highlight_fill = PatternFill("solid", fgColor="FFFACD")

    # ───────── 4) 제목 영역 ─────────
    title = f"{year}년 {month}월 근무표"
    # 팀별보기면 맨 앞(컬럼1)에 팀 컬럼 1개 추가 → static_cols 4→5.
    # 배치: 팀(1) · 번호(2) · 이름(3) · 구분(4) · 경력(5). 기본: 번호(1) · 이름(2) · 구분(3) · 경력(4).
    # 날짜 시작열·spacer·요약열은 static_cols 에서 파생되므로 자동 보정.
    static_cols = 5 if team_view else 4
    idx_col  = 2 if team_view else 1  # 번호 컬럼
    name_col = 3 if team_view else 2  # 이름 컬럼
    role_col = name_col + 1           # 구분
    exp_col  = name_col + 2           # 경력
    spacer_cols = 2
    leave_cols = len(leave_labels)
    total_cols = static_cols + days_in_month + spacer_cols + summary_cols + leave_cols

    ws.merge_cells(start_row=2, start_column=1, end_row=3, end_column=total_cols)
    ws.cell(row=2, column=1, value=title).font = title_font
    ws.cell(row=2, column=1).alignment = center

    # ───────── 5) 헤더 행 ─────────
    header_row = 7
    if team_view:
        ws.cell(row=header_row, column=1, value="팀").font = header_font
        ws.cell(row=header_row, column=2, value="번호").font = header_font
    else:
        ws.cell(row=header_row, column=1, value="번호").font = header_font
    ws.cell(row=header_row, column=name_col, value="이름").font = header_font
    ws.cell(row=header_row, column=role_col, value="구분").font = header_font
    ws.cell(row=header_row, column=exp_col, value="경력").font = header_font

    for c in range(1, static_cols + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.alignment = center
        cell.border = border_all
        cell.fill = gray_fill

    # ★ 요일 행 — 헤더 바로 위(그동안 비어 있던 자리)에 넣는다. `header_row` 를 건드리면
    #   본문·풋터 오프셋이 전부 파생돼 어긋나므로 **행을 늘리지 않고** 빈 줄을 쓴다.
    wd_row = header_row - 1
    for d in range(1, days_in_month + 1):
        col = static_cols + d
        _fc = _day_font_color(year, month, d, holidays)
        wcell = ws.cell(row=wd_row, column=col, value=_WD[calendar.weekday(year, month, d)])
        wcell.font = Font(bold=True, size=10, color=_fc) if _fc else Font(bold=True, size=10)
        wcell.alignment = center
        wcell.border = border_all
        wcell.fill = gray_fill

        cell = ws.cell(row=header_row, column=col, value=d)
        # 일자도 같은 색 — 병원 시트가 두 행을 같은 색으로 칠한다(실측).
        cell.font = Font(bold=True, size=12, color=_fc) if _fc else header_font
        cell.alignment = center
        cell.border = border_all
        cell.fill = gray_fill

    tail_start_col = static_cols + days_in_month + spacer_cols + 1
    for i, lab in enumerate(tail_labels):
        col = tail_start_col + i
        cell = ws.cell(row=header_row, column=col, value=lab)
        cell.font = header_font
        cell.alignment = center
        cell.border = border_all
        cell.fill = gray_fill
        # ★ 본문 셀과 **같은 규약**으로 채운다 — 색이 있는 코드는 전부.
        #   본문만 칠하고 집계는 안 칠하면 같은 코드가 두 형식으로 보여 오히려 헷갈린다.
        _bg = code_bg.get(lab)
        if _bg:
            cell.fill = PatternFill("solid", fgColor=_bg)
            cell.font = Font(bold=True, size=12, color=_readable_text_color(_bg))

    # 요일 행의 빈 구간(정적 컬럼·스페이서·요약열)도 같은 배경·테두리로 이어 붙인다.
    #   그렇지 않으면 일자 위에만 띠가 있고 좌우가 끊겨 표가 깨져 보인다.
    for _c in list(range(1, static_cols + 1)) + list(
            range(static_cols + days_in_month + 1, tail_start_col + summary_cols)):
        _cell = ws.cell(row=wd_row, column=_c)
        _cell.border = border_all
        _cell.fill = gray_fill

    leave_start_col = tail_start_col + summary_cols
    for i, lab in enumerate(leave_labels):
        cell = ws.cell(row=header_row, column=leave_start_col + i, value=lab)
        cell.font = header_font
        cell.alignment = center
        cell.border = border_all
        cell.fill = gray_fill
    if leave_labels:
        # ★★ **병합하지 않는다.** `merge_cells` 는 좌상단을 뺀 나머지를 `MergedCell` 로
        #   갈아치우면서 스타일을 버려, 마지막 열에 테두리가 빠지고 박스가 깨진다
        #   (병합 전에 스타일을 걸어도 마찬가지다 — 실측으로 두 순서 다 확인).
        #   `centerContinuous` 는 셀을 합치지 않고 **글자만 가로로 펼쳐** 가운데 정렬한다.
        #   테두리·배경이 셀마다 그대로 남아 박스가 온전하다.
        _span = Alignment(horizontal="centerContinuous", vertical="center")
        for i in range(leave_cols):
            _c = ws.cell(row=wd_row, column=leave_start_col + i)
            _c.border = border_all
            _c.fill = gray_fill
            _c.alignment = _span
        _lh = ws.cell(row=wd_row, column=leave_start_col, value="연차사용현황")
        _lh.font = header_font
        _lh.alignment = _span

    # 열 너비 (팀별보기: A=팀8, B=번호5 / 기본: A=번호5)
    if team_view:
        ws.column_dimensions['A'].width = 8  # 팀
        ws.column_dimensions['B'].width = 5  # 번호
    else:
        ws.column_dimensions['A'].width = 5  # 번호
    ws.column_dimensions[get_column_letter(name_col)].width = 12  # 이름
    ws.column_dimensions[get_column_letter(role_col)].width = 6   # 구분
    ws.column_dimensions[get_column_letter(exp_col)].width = 6    # 경력
    for d in range(1, days_in_month + 1):
        ws.column_dimensions[get_column_letter(static_cols + d)].width = 4
    for s in range(spacer_cols):
        ws.column_dimensions[get_column_letter(static_cols + days_in_month + 1 + s)].width = 3
    for i, lab in enumerate(tail_labels):
        col_letter = get_column_letter(tail_start_col + i)
        ws.column_dimensions[col_letter].width = 6 if len(lab) > 1 else 5
    for i, lab in enumerate(leave_labels):
        ws.column_dimensions[get_column_letter(leave_start_col + i)].width = 9

    # ───────── 6) 본문 ─────────
    start_row = header_row + 1
    daily_counts = {d: {code: 0 for code in tail_labels} for d in range(1, days_in_month + 1)}

    def write_nurse_row(n, r: int, idx: int):
        """간호사 1명을 r 행에 작성하고 daily_counts 를 누적한다."""
        is_current_user = (str(n.nurse_id) == str(current_user.nurse_id))

        ws.cell(row=r, column=idx_col, value=idx)
        ws.cell(row=r, column=name_col, value=n.name)
        ws.cell(row=r, column=role_col, value=n.role)
        ws.cell(row=r, column=exp_col, value=n.experience)
        # 팀 컬럼(col1)은 본문 작성 후 팀 그룹 경계로 세로병합하므로 여기선 비워둔다.

        for c in range(1, static_cols + 1):
            cell = ws.cell(row=r, column=c)
            cell.alignment = center
            cell.border = border_all
            if is_current_user:
                cell.fill = highlight_fill

        row_counts = {code: 0 for code in tail_labels}
        schedule_map = by_nurse.get(n.nurse_id, {})
        #: 이 사람의 **날짜별** 고정근무 코드 — 그 칸만 배경을 뺀다.
        #:   코드 단위로 빼면 남의 D 까지 지워지고, 날짜를 안 보면 월 중 고정근무가
        #:   시작·변경된 사람의 일반 배정까지 지워진다.
        _my_fixed_days = fixed_by_nurse.get(str(n.nurse_id), _EMPTY_MAP)

        for d in range(1, days_in_month + 1):
            shift_code = schedule_map.get(d, '-')
            cell = ws.cell(row=r, column=static_cols + d, value=shift_code)
            cell.alignment = center
            cell.border = border_all
            # ★★ 배정 영역은 **대표 코드에만** 배경을 칠한다(3교대 D/E/N + OFF 교환 대상).
            #   특수코드까지 칠하면 표가 얼룩덜룩해 정작 봐야 할 원티드 표시가 묻힌다.
            #   (범례·집계는 이 제한을 받지 않는다 — 거기선 색이 코드를 가리키는 축이다.)
            _code = str(shift_code).strip()
            _bg = (
                code_bg.get(_code)
                if (
                    _code in fill_codes
                    and _code not in _my_fixed_days.get(d, _EMPTY_SET)
                )
                else None
            )
            if _bg:
                cell.fill = PatternFill("solid", fgColor=_bg)
                cell.font = Font(color=_readable_text_color(_bg))
            if is_current_user and not _bg:
                # 색이 있는 칸은 덮지 않는다 — 덮으면 코드 색이 사라진다.
                cell.fill = highlight_fill

            # 원티드 표시 — 배경은 그대로 두고 **글자색만** 덮는다. 누가 정한 칸인지를
            #   나타내므로 코드 색(무슨 근무인가)과 축이 다르다. 볼드는 쓰지 않는다.
            #   ★★ **요청 코드와 실제 배정이 같을 때만** 칠한다. 표시하려는 것은
            #     '반영된 내역' 이므로, 신청했지만 다른 근무가 들어간 칸까지 칠하면
            #     들어준 것과 못 들어준 것이 같은 색이 되어 구분이 사라진다.
            #     (확정 원티드는 하드 고정이라 대개 일치하지만, 생성 뒤 수동 수정으로
            #      어긋날 수 있어 여기서도 같은 기준을 적용한다.)
            #   글자색 축은 **둘**이고 빨강이 우선한다:
            #     · 빨강 = **확정 원티드로 들어간 쉬는 날**. 확정 원티드에 있고 실제 배정이
            #       그 코드이며, 코드가 **OFF축이거나 연차 계열**일 때만 칠한다.
            #       (제출 테이블과는 대조하지 않는다 — 위 2-c 주석)
            #       ★ 원티드로 받은 근무(D/E/N)·교육·공가까지 빨갛게 하면 "쉬는 날" 신호가
            #         흐려진다. 실측 운영 중환자실-RN 2026-10 확정원티드 164건 중
            #         OFF·연차가 123건, 특수코드 32건, 근무 9건이다.
            #     · 파랑 = 기타 특수코드(보수교육·직무교육·공가·병가 등). 연차는 자체 축이라
            #       여기 안 든다(배경을 칠한다).
            #   ★ 고정근무 코드(`DA`·`DD` 등)는 어느 쪽도 아니다. 그 사람에겐 상시 근무라
            #     특수코드로 묶으면 한 줄이 통째로 파래진다.
            #   ★ **요청 코드와 실제 배정이 같을 때만** 칠한다. 확정 원티드는 하드 고정이라
            #     대개 일치하지만 생성 뒤 수동 수정으로 어긋날 수 있고, 그때 칠하면
            #     들어준 것과 못 들어준 것이 같은 색이 되어 구분이 사라진다.
            _w = wanted_req.get((str(n.nurse_id), d))
            if _w and _w == _code and (
                _code in off_axis_codes or _code in leave_unit_codes
            ):
                cell.font = Font(color=_C_WANTED)
            elif (
                _code and _code != "-"
                and _code not in den_codes
                and _code not in off_axis_codes
                and _code not in leave_unit_codes
                # 고정근무 제외는 두 겹이다 — 근무 코드는 전역(남이 하루 받아도 근무다),
                #   그 밖의 코드는 **그 사람·그 날**에 한해서만.
                and _code not in fixed_codes
                and _code not in _my_fixed_days.get(d, _EMPTY_SET)
            ):
                cell.font = Font(color=_C_SPECIAL)

            base = to_base(shift_code)
            if base in row_counts:
                row_counts[base] += 1
                daily_counts[d][base] += 1

        # 요약 열: 0도 그대로 표시 (빈칸 → 0)
        for i, lab in enumerate(tail_labels):
            col = tail_start_col + i
            value = row_counts[lab]
            cell = ws.cell(row=r, column=col, value=value)  # ← 변경: value 그대로
            cell.alignment = center
            cell.border = border_all
            if is_current_user:
                cell.fill = highlight_fill

        # 연차 3열 — `전월잔여` 는 장부에서, `사용`·`당월잔여` 는 **이 근무표**에서.
        #   ★ 장부의 `used`/`closing` 을 읽지 않는 이유: draft 를 내려받아도 "이대로
        #     마감하면 잔여가 얼마가 되는지" 가 보여야 한다. 장부는 마감본 전용이다.
        #   ★ 행이 없는 간호사(전월 기록 없음)는 **빈칸**으로 둔다 — 0 으로 찍으면
        #     "연차가 없다" 로 오해된다.
        if leave_labels:
            _op = leave_opening.get(str(n.nurse_id))
            _us = leave_used.get(str(n.nurse_id)) if leave_used is not None else None
            if _op is None:
                _vals = [None, None, None]
            elif leave_used is None:
                # 계산 실패 — 전월잔여(확정값)만 보여주고 나머지는 비운다.
                _vals = [float(_op), None, None]
            else:
                _u = _us if _us is not None else 0
                _vals = [float(_op), float(_u), float(_op) - float(_u)]
            for i, v in enumerate(_vals):
                cell = ws.cell(row=r, column=leave_start_col + i, value=v)
                cell.alignment = center
                cell.border = border_all
                if is_current_user:
                    cell.fill = highlight_fill

    # 본문은 팀별보기·기본 모두 동일한 연속 행으로 작성한다(레이아웃 동일). 팀별보기면
    # 작성하면서 같은 팀 라벨의 연속 행 구간을 모아 컬럼2를 세로 병합한다.
    for idx, n in enumerate(nurses, start=1):
        write_nurse_row(n, start_row + idx - 1, idx)
    last_row = start_row + len(nurses) - 1

    if team_view and nurses:
        # 정렬된 nurses 를 순회하며 같은 team_id 의 연속 구간(첫행~끝행)을 모아 컬럼1을
        # 세로 병합한다. 그룹 키는 team_of(gated team_id, None=미등록) — 팀명이 우연히 같아도
        # 다른 팀은 합치지 않는다. 표시 라벨은 nurse_team_label(팀명/"미등록") 사용.
        col_team = 1
        _UNASSIGNED = object()  # None 과 "구간 미시작" 을 구분하기 위한 센티넬
        run_key = _UNASSIGNED
        run_start = start_row
        run_label = "미등록"
        for idx, n in enumerate(nurses):
            r = start_row + idx
            tid = team_of.get(str(n.nurse_id))  # int|None
            label = nurse_team_label.get(str(n.nurse_id), "미등록")
            if run_key is _UNASSIGNED:
                run_key, run_start, run_label = tid, r, label
            elif tid != run_key:
                _merge_team_cell(ws, col_team, run_start, r - 1, run_label,
                                 center, border_all, gray_fill)
                run_key, run_start, run_label = tid, r, label
        _merge_team_cell(ws, col_team, run_start, last_row, run_label,
                         center, border_all, gray_fill)

    # ───────── 7) 풋터 ─────────
    # 라벨 위치는 이름/구분 컬럼에 맞춰 팀별보기 시 한 칸씩 밀린다(레이아웃 동일).
    footer_start = last_row + 2
    ws.cell(row=footer_start, column=name_col, value="일일 근무 현황").font = header_font

    def write_footer_row(label: str, values: list[int], row_idx: int):
        lab_cell = ws.cell(row=row_idx, column=role_col, value=label)
        lab_cell.font = header_font
        lab_cell.alignment = center
        # ★ 요약 열·본문과 같은 규약 — 색이 있는 코드는 전부 채운다.
        _bg = code_bg.get(label)
        if _bg:
            lab_cell.fill = PatternFill("solid", fgColor=_bg)
            lab_cell.font = Font(bold=True, size=12, color=_readable_text_color(_bg))
        for c in range(1, static_cols):
            ws.cell(row=row_idx, column=c).border = border_all

        for d in range(1, days_in_month + 1):
            val = values[d - 1]
            cell = ws.cell(row=row_idx, column=static_cols + d, value=val)  # ← 변경: val 그대로 (0도 표시)
            cell.alignment = center
            cell.border = border_all

        for i in range(spacer_cols + summary_cols + leave_cols):
            col = static_cols + days_in_month + 1 + i
            ws.cell(row=row_idx, column=col).border = border_all

    for i, lab in enumerate(tail_labels):
        row_idx = footer_start + 1 + i
        vals = [daily_counts[d][lab] for d in range(1, days_in_month + 1)]
        write_footer_row(lab, vals, row_idx)

    # ───────── 8) 테두리 보정 ─────────
    max_col = tail_start_col + len(tail_labels) + leave_cols - 1
    for row in ws.iter_rows(min_row=wd_row, max_row=footer_start + len(tail_labels) + 1,
                            min_col=1, max_col=max_col):
        for cell in row:
            if cell.value is not None and (cell.border is None or cell.border.left.style is None):
                cell.border = border_all

    # ★★ 연차 장부가 현재 마감본과 어긋나면 그 사실을 **보이게** 한다.
    #   재계산 훅은 fail-open 이라 실패해도 근무표는 커밋된다(데드락·유니크 경합 등).
    #   그때 `전월잔여` 가 옛값인 채 남는데, 숫자만 보면 맞는지 알 수 없다.
    _warn_msg = None
    if leave_error:
        _warn_msg = f"※ {leave_error} — 연차 값이 불완전합니다"
    elif leave_labels and leave_stale:
        _warn_msg = "※ 연차 잔여가 최신 마감본과 다릅니다 — 재계산 필요"
    if _warn_msg:
        # ★ 장부가 없는 병동이라 열이 아예 없을 때도 **실패는 알린다** —
        #   조용히 빠진 것과 원래 없는 것을 사용자가 구분할 수 있어야 한다.
        _warn = ws.cell(row=footer_start, column=tail_start_col, value=_warn_msg)
        _warn.font = Font(bold=True, color=_C_SUN_HOL)
        print(f"[Excel] {schedule_id} {_warn_msg}")

    # ───────── 저장 ─────────
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue(), team_view


def export_members_excel_bytes(office_id: str) -> bytes:
    """ADM용 멤버 목록 엑셀 생성.

    - 입력: office_id
    - 컬럼: 대분류, 중분류, 소분류, 부서명, 사번, 직원명, 계정 ID, 직무, 경력, 수간호사여부, 입사일, 생년월일, 연락처
    - 반환: 생성된 xlsx 바이트
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

    rows = msdb_manager.fetch_all(Member.member_export_by_office(), params=(str(office_id),)) or []

    headers = [
        ("big_kind_name", "대분류"),
        ("middle_kind_name", "중분류"),
        ("small_kind_name", "소분류"),
        ("mb_part_name", "부서명"),
        ("OfficeEmpNum", "사번"),
        ("MemberID", "계정 ID"),
        ("EmployeeName", "직원명"),
        ("duty", "직무"),
        ("career", "경력"),
        ("headnurse", "수간호사여부"),
        ("joindate", "입사일"),
        ("DateOfBirth", "생년월일"),
        ("PortableTel", "연락처"),
        ("Gender", "성별"),
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = "구성원"

    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center")
    header_font = Font(bold=True)
    gray = PatternFill("solid", fgColor="DEE2E6")
    thin = Side(style="thin", color="000000")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)

    # 안내 문구 (맨 윗줄, 노란색 배경)
    guide_text = '사번 컬럼부터 우측으로 필요한 정보를 복사해 템플릿에 그룹(병동)별로 추가하여 업로드 하시면 됩니다'
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    gcell = ws.cell(row=1, column=1, value=guide_text)
    gcell.alignment = left
    gcell.fill = PatternFill("solid", fgColor="FFF59D")  # 연노랑
    gcell.border = border_all

    # 헤더 (2행부터)
    for i, (_, label) in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=i, value=label)
        cell.font = header_font
        cell.alignment = center
        cell.fill = gray
        cell.border = border_all

    # 데이터 (3행부터)
    for r_idx, row in enumerate(rows, start=3):
        for c_idx, (key, _) in enumerate(headers, start=1):
            # pyodbc.Row 또는 dict 형태 지원
            try:
                val = row[key]
            except Exception:
                val = row.get(key) if hasattr(row, 'get') else None
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.alignment = left
            cell.border = border_all

    # 약간의 폭 조정
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[chr(64 + col)].width = 16

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.read()


def _excel_upsert_allowed(db: Session, nurse, allowed_shifts) -> None:
    """엑셀 업로드의 allowed_shifts 를 period(SSOT) 경유로 기록 + 캐시 단방향 투영.

    엑셀은 월 컨텍스트가 없어 valid_from=today(현재값 변경). upsert 가 동일값이면 no-op,
    캐시(nurses.allowed_shifts)는 내부에서 투영하므로 직접 대입은 제거.
    """
    from datetime import date as _date
    from services.nurse_period_resolver import upsert_period
    from db.models import NurseAllowedShiftPeriod
    upsert_period(
        db, NurseAllowedShiftPeriod, str(nurse.nurse_id), _date.today(),
        "allowed_shifts", allowed_shifts if allowed_shifts is not None else [],
        nurse=nurse, cache_attr="allowed_shifts", source="excel_upload",
        carry_attrs=["fixed_shift"],
    )


def upload2_confirm(rows: List[Dict[str, Any]], user: UserSchema, db: Session, target_group_id: str) -> Dict[str, Any]:
    """업로드2: 검증된 행을 nurses 테이블에 저장 (신규/업데이트)"""
    print("[upload2_confirm] 함수 시작")
    print(f"  • target_group_id: {target_group_id}")
    print(f"  • rows 개수: {len(rows)}")

    try:
        if not target_group_id:
            print("[ERROR] target_group_id 누락")
            return {"success": 0, "errors": [{"row": 0, "reason": "group_id가 필요합니다."}]}

        saved = 0
        updated = 0
        errors = []  # ★★★ 이 한 줄이 핵심! NameError 방지 ★★★

        for idx, item in enumerate(rows, 1):
            print(f"[행 {idx}] 처리: account_id={item.get('account_id')}")

            account_id = str(item.get('account_id', '')).strip()
            if not account_id:
                errors.append({"row": item.get('row', 0), "reason": "account_id 누락"})
                continue

            name = str(item.get('name', '')).strip() or account_id
            emp_num = str(item.get('emp_num', '')).strip() or None
            role = str(item.get('role', 'RN')).strip()
            experience = item.get('experience')
            if not isinstance(experience, int):
                experience = 1

            is_head_nurse = bool(item.get('is_head_nurse', False))

            joining_dt = None
            if item.get('joining_date'):
                try:
                    joining_dt = pd.to_datetime(item['joining_date']).to_pydatetime()
                except:
                    pass

            birth_date = str(item.get('birth_date', '')).strip()[:10] or None
            phone_number = str(item.get('phone_number', '')).strip()[:20] or None
            gender = str(item.get('gender', '')).strip()[:3] or None
            email = str(item.get('email', '')).strip()[:100] or None

            allowed_shifts = item.get('allowed_shifts', []) or []
            work_shifts = item.get('work_shifts', []) or []

            nurse_id = item.get('nurse_id')
            # if not nurse_id or not isinstance(nurse_id, str) or len(str(nurse_id).strip()) < 8:
            #     nurse_id = str(uuid.uuid4())
            print(f"   → nurse_id 자동 생성: {nurse_id}")

            existing = db.query(NurseModel).filter(
                NurseModel.account_id == account_id
            ).first()

            if existing:
                print(f"   → 기존 레코드 업데이트 (nurse_id={existing.nurse_id})")
                if name and existing.name != name:
                    existing.name = name
                existing.emp_num = emp_num
                existing.role = role
                existing.experience = experience
                existing.is_head_nurse = is_head_nurse
                existing.office_id = user.office_id
                if joining_dt:
                    existing.joining_date = joining_dt
                existing.birth_date = birth_date
                existing.phone_number = phone_number
                existing.gender = gender
                existing.email = email
                # allowed_shifts 는 period(SSOT) 경유 — 캐시 직접쓰기는 생성기(period as-of)와
                #   어긋난다(P2). 엑셀은 월 컨텍스트가 없어 valid_from=today(현재값 변경).
                #   upsert 가 nurses.allowed_shifts 캐시에 단방향 투영한다.
                _excel_upsert_allowed(db, existing, allowed_shifts)
                existing.work_shifts = work_shifts
                updated += 1
            else:
                print("   → 신규 등록")
                try:
                    seq_next = get_next_sequence(target_group_id, 1, db, role=role)
                    new_nurse = NurseModel(
                        nurse_id=nurse_id,
                        group_id=target_group_id,
                        office_id=user.office_id,
                        account_id=account_id,
                        emp_num=emp_num,
                        name=name,
                        experience=experience,
                        role=role,
                        level_='일반',
                        is_head_nurse=is_head_nurse,
                        allowed_shifts=allowed_shifts,
                        personal_off_adjustment=0,
                        preceptor_id=None,
                        joining_date=joining_dt,
                        sequence=seq_next,
                        active=1,
                        birth_date=birth_date,
                        phone_number=phone_number,
                        gender=gender,
                        email=email,
                        work_shifts=work_shifts,
                    )
                    db.add(new_nurse)
                    # 신규도 allowed_shifts 를 period 에 시드 — 생성기 per-day 해석이 캐시 폴백이
                    #   아니라 명시 구간을 읽게(P2). gap 폴백도 동작하지만 SSOT 일관성 위해 기록.
                    _excel_upsert_allowed(db, new_nurse, allowed_shifts)
                    saved += 1
                except Exception as inner_e:
                    errors.append({"row": item.get('row', 0), "reason": str(inner_e)})
                    print(f"   → 신규 등록 실패: {str(inner_e)}")

        print(f"[COMMIT 직전] saved={saved}, updated={updated}, errors={len(errors)}")
        db.commit()
        print("[COMMIT 성공]")
        return {"success": saved + updated, "saved": saved, "updated": updated, "errors": errors}

    except Exception as e:
        print(f"[전체 예외] {type(e).__name__}: {str(e)}")
        db.rollback()
        return {"success": 0, "errors": [{"row": 0, "reason": str(e)}]}


# def direct_validate_and_confirm(rows: List[Dict[str, Any]], user: UserSchema, db: Session, group_id: str) -> Dict[str, Any]:
#     """
#     직접 입력된 간호사 데이터 검증 + 저장 (엑셀 없이 사용)
#     """
#     try:
#         office_id = user.office_id

#         # 허용 계정 목록 조회
#         rows_allowed = msdb_manager.fetch_all(Member.member_accounts_by_office(), params=(str(office_id),))
#         allowed = {}
#         for r in rows_allowed or []:
#             acc = str(r.get('account_id', '')).strip()
#             if acc:
#                 allowed[acc] = (
#                     str(r.get('name', '')).strip(),
#                     r.get('EmpAuthGbn'),
#                     str(r.get('nurse_id', uuid.uuid4())).strip()
#                 )

#         normalized = []
#         errors = []
#         head_count = 0
#         acc_set = set()

#         for i, row in enumerate(rows):
#             ridx = i + 1
#             row_errs = []

#             account_id = str(row.get('account_id', '')).strip()
#             name = str(row.get('name', '')).strip()
#             role = str(row.get('role', 'RN')).strip()
#             exp_val = row.get('experience')
#             try:
#                 exp_val = int(exp_val) if exp_val is not None else None
#             except:
#                 row_errs.append("경력은 숫자여야 합니다.")

#             is_head = str(row.get('is_head_nurse', 'N')).upper() == 'Y'
#             if is_head:
#                 head_count += 1

#             joining_dt = row.get('joining_date')

#             if not account_id:
#                 row_errs.append("계정 ID 누락")
#             elif not re.match(r'^\S{1,50}$', account_id):
#                 row_errs.append("계정 ID 형식 오류")
#             elif account_id not in allowed:
#                 row_errs.append(f"허용되지 않은 계정: {account_id}")

#             if not name:
#                 name = allowed.get(account_id, ('', None, ''))[0]
#                 if not name:
#                     row_errs.append("직원명 누락")

#             if not role:
#                 row_errs.append("직무 누락")

#             if account_id in acc_set:
#                 row_errs.append("중복 계정 ID")
#             else:
#                 acc_set.add(account_id)

#             normalized.append({
#                 'row': ridx,
#                 'emp_num': row.get('emp_num'),
#                 'account_id': account_id,
#                 'name': name,
#                 'role': role,
#                 'experience': exp_val,
#                 'is_head_nurse': is_head,
#                 'joining_date': joining_dt,
#                 'nurse_id': allowed.get(account_id, ('', '', str(uuid.uuid4())))[2],
#                 'birth_date': row.get('birth_date'),
#                 'phone_number': row.get('phone_number'),
#                 'allowed_shifts': row.get('allowed_shifts', []),
#                 'gender': row.get('gender')
#             })

#             if row_errs:
#                 errors.append({'row': ridx, 'reason': '; '.join(row_errs)})

#         # 글로벌 검증
#         existing_heads = db.query(NurseModel).filter(
#             NurseModel.office_id == office_id,
#             NurseModel.group_id == group_id,
#             NurseModel.is_head_nurse == 1
#         ).count()

#         if head_count == 0 and existing_heads == 0:
#             errors.append({'row': 0, 'reason': '수간호사는 최소 1명 이상이어야 합니다.'})

#         existing_accs = db.query(NurseModel.account_id).filter(NurseModel.account_id.in_(list(acc_set))).all()
#         if existing_accs:
#             for (acc,) in existing_accs:
#                 errors.append({'row': 0, 'reason': f'이미 등록된 계정 ID: {acc}'})

#         if errors:
#             return {
#                 'success': 0,
#                 'errors': errors,
#                 'rows': normalized,
#                 'summary': {'total': len(normalized), 'error_count': len(errors)}
#             }

#         # group_id 강제 체크 (ADM 대비)
#         if not group_id:
#             raise ValueError("group_id가 누락되었습니다. 관리자 계정은 반드시 group_id를 지정해야 합니다.")

#         # 검증 통과 → 저장
#         return upload2_confirm(normalized, user, db, group_id)

#     except Exception as e:
#         return {"success": 0, "errors": [{"row": 0, "reason": str(e)}]}


# def integrated_member_and_nurse_register(
#     members: List[Dict[str, Any]],
#     user: UserSchema,
#     db: Session,
#     group_id: str
# ) -> Dict[str, Any]:
#     """
#     직접 입력으로 신규 직원 계정 생성 + 근무자 등록 통합 처리
#     """
#     try:
#         office_code = user.office_id
#         emp_seq_no = getattr(user, 'EmpSeqNo', user.nurse_id or '')
#         reg_date = datetime.now()

#         if not members:
#             return {"success": False, "errors": [{"reason": "입력 데이터가 없습니다."}]}

#         # group_id 필수 체크 (ADM 대비)
#         if not group_id:
#             return {"success": False, "errors": [{"reason": "group_id가 누락되었습니다. 대상 병동을 선택해주세요."}]}

#         # 1~4 단계: member 임시 저장 → 외부 API 호출 (기존 그대로)
#         df = pd.DataFrame(members)
#         df['num'] = range(1, len(df) + 1)

#         rename_map = {
#             '사번': 'EmpNum',
#             '회원 아이디': 'MemberID',
#             '이름': 'EmployeeName',
#             '성별': 'Gender',
#             '생년월일': 'Birthday',
#             '입사년월': 'JoinDate',
#             '전화번호': 'Tel',
#             '휴대폰 번호': 'PortableTel',
#             '이메일': 'Email',
#             '주소': 'Address',
#             '부서장': 'Manager',
#             '상위부서': 'Depth1',
#             '하위부서1': 'Depth2',
#             '하위부서2': 'Depth3',
#             '직위': 'Posin',
#             '경력': 'career',
#             '직무': 'duty',
#             '수간호사여부': 'headnurse',
#             '킵여부': 'nightkeep'
#         }
#         df = df.rename(columns=rename_map)

#         # 타입 변환 및 검증 (기존 로직 그대로 유지)
#         # ... (중략: 필수값 체크, 이메일/생년월일/성별/경력/부서 검증 등) ...

#         # 임시 테이블 저장 및 외부 API 호출 (기존 그대로)
#         # ... (중략) ...

#         # 5. nurses 등록
#         nurse_rows = []
#         for row in members:
#             nurse_rows.append({
#                 'emp_num': row.get('사번'),
#                 'account_id': row.get('회원 아이디'),
#                 'name': row.get('이름'),
#                 'role': row.get('직무', 'RN'),
#                 'experience': row.get('경력'),
#                 'is_head_nurse': str(row.get('수간호사여부', 'N')).upper() == 'Y',
#                 'joining_date': row.get('입사년월'),
#                 'birth_date': row.get('생년월일'),
#                 'phone_number': row.get('휴대폰 번호'),
#                 'gender': row.get('성별')
#             })

#         # 검증 + 저장
#         nurse_result = direct_validate_and_confirm(nurse_rows, user, db, group_id)

#         return {
#             "success": nurse_result.get('success', 0) > 0,
#             "member_count": len(members),
#             "nurse_result": nurse_result,
#             "message": "신규 직원 계정 생성 및 근무자 등록 완료" if nurse_result.get('success', 0) > 0 else "nurses 등록 실패"
#         }

#     except Exception as e:
#         return {"success": False, "errors": [{"reason": f"처리 중 오류 발생: {str(e)}"}]}


# def integrated_member_and_nurse_register(
#     members: List[Dict[str, Any]],
#     user: UserSchema,
#     db: Session,
#     group_id: str
# ) -> Dict[str, Any]:
#     """
#     직접 입력으로 신규 직원 계정 생성 + 근무자 등록 통합 처리
#     - member 테이블 생성 → 외부 API 동기화 → nurses 테이블 등록
#     """
#     try:
#         office_code = user.office_id
#         emp_seq_no = getattr(user, 'EmpSeqNo', user.nurse_id or '')
#         reg_date = datetime.now()

#         if not members:
#             return {"success": False, "errors": [{"reason": "입력 데이터가 없습니다."}]}

#         # group_id 필수 체크 (ADM 대비)
#         if not group_id:
#             return {"success": False, "errors": [{"reason": "group_id가 누락되었습니다. 대상 병동을 선택해주세요."}]}

#         # 1. DataFrame 변환 및 컬럼 매핑
#         df = pd.DataFrame(members)
#         df['num'] = range(1, len(df) + 1)

#         rename_map = {
#             '사번': 'EmpNum',
#             '회원 아이디': 'MemberID',
#             '이름': 'EmployeeName',
#             '성별': 'Gender',
#             '생년월일': 'Birthday',
#             '입사년월': 'JoinDate',
#             '전화번호': 'Tel',
#             '휴대폰 번호': 'PortableTel',
#             '이메일': 'Email',
#             '주소': 'Address',
#             '부서장': 'Manager',
#             '상위부서': 'Depth1',
#             '하위부서1': 'Depth2',
#             '하위부서2': 'Depth3',
#             '직위': 'Posin',
#             '경력': 'career',
#             '직무': 'duty',
#             '수간호사여부': 'headnurse',
#             '킵여부': 'nightkeep'
#         }
#         df = df.rename(columns=rename_map)

#         # 타입 변환
#         df['Birthday'] = pd.to_numeric(df.get('Birthday'), errors='coerce').astype('Int64')
#         df['career'] = pd.to_numeric(df.get('career'), errors='coerce').astype('Int64')

#         # 2. 검증 (기존 로직 그대로 유지 - 생략 가능 시 주석 처리)
#         error_rows = []
#         # ... (중략: 중복, 필수값, 이메일, 생년월일, 성별, 수간호사, 경력, 부서, MemberID 중복 체크 등) ...

#         if error_rows:
#             error_df = pd.concat(error_rows, ignore_index=True).replace({np.nan: ''})
#             error_df.drop_duplicates(inplace=True)
#             return {"success": False, "errors": error_df.to_dict(orient='records')}

#         # 3. 임시 테이블 저장
#         df['OfficeCode'] = office_code
#         df['EmpSeqNo'] = emp_seq_no
#         df['RegDate'] = reg_date
#         df = df.replace({np.nan: ''})

#         insert_cols = [
#             'num', 'OfficeCode', 'EmpSeqNo', 'EmpNum', 'MemberID', 'EmployeeName', 'Gender',
#             'Birthday', 'JoinDate', 'Tel', 'PortableTel', 'Email', 'Address', 'Manager',
#             'Depth1', 'Depth2', 'Depth3', 'Posin', 'RegDate', 'career', 'duty', 'headnurse', 'nightkeep'
#         ]
#         df_insert = df[insert_cols]
#         data_to_insert = [tuple(row) for row in df_insert.itertuples(index=False)]

#         msdb_manager.execute(Setting.delete_member(), params=(office_code, emp_seq_no))
#         msdb_manager.bulk_execute(Setting.insert_member(), data_to_insert)

#         # 모바일 설정
#         member_ids = [mid for mid in df["MemberID"].tolist() if mid]
#         mobile_params = [(mid, reg_date) for mid in member_ids]
#         if mobile_params:
#             msdb_manager.bulk_execute(Setting.insert_mobile_user_setting_list(), mobile_params)

#         # 4. 외부 API 호출 (Member 테이블 실제 생성)
#         token = create_access_token(data={"clientSecret": os.getenv("CLIENT_SECRET"), "clientId": os.getenv("CLIENT_ID")})
#         response = requests.post(
#             "https://gw.meditong.com/bizadmin/setting/member_excel_ai_ok.asp",
#             data=f"officeCode={office_code}&EmpSeqNo={emp_seq_no}&Token={token}",
#             headers={'Content-Type': 'application/x-www-form-urlencoded'}
#         )
#         if response.status_code != 200:
#             return {"success": False, "errors": [{"reason": f"외부 동기화 API 실패: {response.text}"}]}

#         # 5. nurses 테이블 등록
#         nurse_rows = []
#         for row in members:
#             nurse_rows.append({
#                 'emp_num': row.get('사번'),
#                 'account_id': row.get('회원 아이디'),
#                 'name': row.get('이름'),
#                 'role': row.get('직무', 'RN'),
#                 'experience': row.get('경력'),
#                 'is_head_nurse': str(row.get('수간호사여부', 'N')).upper() == 'Y',
#                 'joining_date': row.get('입사년월'),
#                 'birth_date': row.get('생년월일'),
#                 'phone_number': row.get('휴대폰 번호'),
#                 'gender': row.get('성별')
#             })

#         # 검증 + 저장 (upload2_confirm 호출)
#         nurse_result = direct_validate_and_confirm(nurse_rows, user, db, group_id)

#         return {
#             "success": nurse_result.get('success', 0) > 0,
#             "member_count": len(members),
#             "nurse_result": nurse_result,
#             "message": "신규 직원 계정 생성 및 근무자 등록 완료" if nurse_result.get('success', 0) > 0 else "nurses 등록 실패"
#         }

#     except Exception as e:
#         return {"success": False, "errors": [{"reason": f"처리 중 오류 발생: {str(e)}"}]}