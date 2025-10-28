"""
RBAC-Lite 권한 관리 서비스

간호사/관리자 권한 체크, 권한 부여/회수, 권한 조회 등의 비즈니스 로직 제공.

Note: AdminUser 테이블은 제거되고 Nurse 테이블에 통합되었습니다.
      모든 권한 관리는 Nurse 테이블을 통해 이루어집니다.
"""

from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_
from datetime import datetime

from db.rbac_models import (
    Permission, UserPermission, 
    UserPermissionScope, PermissionAuditLog
)
from db.models import Nurse, Group


class PermissionChecker:
    """
    권한 체크 헬퍼 클래스
    
    사용자의 특정 리소스/액션에 대한 권한 유무와 스코프를 빠르게 체크.
    """
    
    def __init__(self, db: Session):
        """
        권한 체커 초기화
        
        Args:
            db: SQLAlchemy 세션
        """
        self.db = db
        self._permission_cache: Dict[str, int] = {}  # code -> permission_id 캐시
    
    def has_permission(
        self, 
        nurse_id: str, 
        permission_code: str, 
        group_id: Optional[str] = None
    ) -> bool:
        """
        간호사/관리자의 특정 권한 보유 여부 확인
        
        nurse_id의 사용자가 permission_code 권한을 가지고 있는지 체크.
        group_id가 제공되면 해당 병동에 대한 권한인지 추가 확인.
        
        Args:
            nurse_id: 간호사/관리자 ID (nurses.nurse_id)
            permission_code: 권한 코드 (예: 'NURSE_READ', 'ROSTER_CREATE')
            group_id: 병동 ID (선택, None이면 전체 권한만 체크)
        
        Returns:
            권한이 있으면 True, 없으면 False
        
        Examples:
            >>> checker = PermissionChecker(db)
            >>> checker.has_permission('N001', 'ROSTER_READ')  # 전체 조회 권한
            True
            >>> checker.has_permission('N002', 'NURSE_UPDATE', 'G001')  # G001 병동만
            False
        """
        # 1. 권한 ID 조회 (캐시 활용)
        permission_id = self._get_permission_id(permission_code)
        if permission_id is None:
            return False
        
        # 2. UserPermission 조회
        user_perm = self.db.query(UserPermission).filter(
            and_(
                UserPermission.nurse_id == nurse_id,
                UserPermission.permission_id == permission_id
            )
        ).first()
        
        if not user_perm:
            return False
        
        # 3. 스코프 체크
        if user_perm.scope_type == 'ALL':
            # 전체 병동 권한
            return True
        
        # 4. 특정 병동 권한 체크
        if group_id is None:
            # group_id가 없으면 전체 권한만 인정 (이미 ALL은 통과했으므로 False)
            return False
        
        # 5. UserPermissionScope에서 해당 병동 권한 확인
        scope_exists = self.db.query(UserPermissionScope).filter(
            and_(
                UserPermissionScope.user_permission_id == user_perm.user_permission_id,
                UserPermissionScope.group_id == group_id
            )
        ).first()
        
        return scope_exists is not None
    
    def get_permitted_groups(
        self, 
        nurse_id: str, 
        permission_code: str
    ) -> Optional[List[str]]:
        """
        간호사/관리자가 특정 권한을 가진 병동 목록 조회
        
        Args:
            nurse_id: 간호사/관리자 ID
            permission_code: 권한 코드
        
        Returns:
            - scope_type이 'ALL'이면 None 반환 (전체 병동)
            - scope_type이 'GROUPS'이면 병동 ID 리스트 반환
            - 권한이 없으면 빈 리스트 반환
        
        Examples:
            >>> checker.get_permitted_groups('N001', 'NURSE_READ')
            None  # 전체 병동 권한
            >>> checker.get_permitted_groups('N002', 'ROSTER_UPDATE')
            ['G001', 'G003']  # G001, G003 병동만
            >>> checker.get_permitted_groups('N003', 'NURSE_DELETE')
            []  # 권한 없음
        """
        permission_id = self._get_permission_id(permission_code)
        if permission_id is None:
            return []
        
        user_perm = self.db.query(UserPermission).filter(
            and_(
                UserPermission.nurse_id == nurse_id,
                UserPermission.permission_id == permission_id
            )
        ).first()
        
        if not user_perm:
            return []
        
        if user_perm.scope_type == 'ALL':
            return None  # None은 전체 병동을 의미
        
        # 특정 병동 목록 조회
        scopes = self.db.query(UserPermissionScope).filter(
            UserPermissionScope.user_permission_id == user_perm.user_permission_id
        ).all()
        
        return [scope.group_id for scope in scopes]
    
    def get_user_all_permissions(self, nurse_id: str) -> List[Dict[str, Any]]:
        """
        간호사/관리자의 모든 권한 조회
        
        Args:
            nurse_id: 간호사/관리자 ID
        
        Returns:
            권한 정보 리스트, 각 항목은 다음 구조:
            {
                'permission_code': str,
                'resource': str,
                'action': str,
                'scope_type': str,
                'groups': List[str] or None
            }
        
        Examples:
            >>> checker.get_user_all_permissions('N001')
            [
                {
                    'permission_code': 'NURSE_READ',
                    'resource': 'NURSE',
                    'action': 'READ',
                    'scope_type': 'ALL',
                    'groups': None
                },
                {
                    'permission_code': 'ROSTER_UPDATE',
                    'resource': 'ROSTER',
                    'action': 'UPDATE',
                    'scope_type': 'GROUPS',
                    'groups': ['G001', 'G002']
                }
            ]
        """
        user_perms = self.db.query(UserPermission).options(
            joinedload(UserPermission.permission),
            joinedload(UserPermission.scopes)
        ).filter(
            UserPermission.nurse_id == nurse_id
        ).all()
        
        result = []
        for up in user_perms:
            perm_info = {
                'permission_code': up.permission.code,
                'resource': up.permission.resource,
                'action': up.permission.action,
                'scope_type': up.scope_type,
                'groups': None if up.scope_type == 'ALL' else [s.group_id for s in up.scopes]
            }
            result.append(perm_info)
        
        return result
    
    def _get_permission_id(self, permission_code: str) -> Optional[int]:
        """
        권한 코드로 permission_id 조회 (캐시 활용)
        
        Args:
            permission_code: 권한 코드
        
        Returns:
            permission_id (없으면 None)
        """
        if permission_code in self._permission_cache:
            return self._permission_cache[permission_code]
        
        perm = self.db.query(Permission).filter(
            and_(
                Permission.code == permission_code,
                Permission.is_active == True
            )
        ).first()
        
        if perm:
            self._permission_cache[permission_code] = perm.permission_id
            return perm.permission_id
        
        return None


class PermissionManager:
    """
    권한 관리 클래스
    
    권한 부여, 회수, 수정 등의 관리 작업 수행.
    """
    
    def __init__(self, db: Session):
        """
        권한 매니저 초기화
        
        Args:
            db: SQLAlchemy 세션
        """
        self.db = db
        self.checker = PermissionChecker(db)
    
    def grant_permission(
        self,
        nurse_id: str,
        permission_code: str,
        scope_type: str,
        group_ids: Optional[List[str]] = None,
        granted_by_nurse_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        간호사/관리자에게 권한 부여
        
        Args:
            nurse_id: 대상 간호사/관리자 ID
            permission_code: 권한 코드
            scope_type: 스코프 타입 ('ALL' 또는 'GROUPS')
            group_ids: 병동 ID 리스트 (scope_type='GROUPS'인 경우 필수)
            granted_by_nurse_id: 권한 부여자 nurse_id
            ip_address: 부여자 IP 주소
            user_agent: 부여자 User Agent
        
        Returns:
            {
                'success': bool,
                'message': str,
                'user_permission_id': int
            }
        
        Raises:
            ValueError: 잘못된 파라미터 (예: scope_type='GROUPS'인데 group_ids 없음)
        
        Examples:
            >>> manager = PermissionManager(db)
            >>> manager.grant_permission(
            ...     nurse_id='N002',
            ...     permission_code='ROSTER_READ',
            ...     scope_type='GROUPS',
            ...     group_ids=['G001', 'G002'],
            ...     granted_by_nurse_id='N001'
            ... )
            {'success': True, 'message': '권한이 부여되었습니다.', 'user_permission_id': 15}
        """
        # 검증
        if scope_type == 'GROUPS' and (not group_ids or len(group_ids) == 0):
            raise ValueError("scope_type이 'GROUPS'인 경우 group_ids가 필요합니다.")
        
        if scope_type not in ['ALL', 'GROUPS']:
            raise ValueError("scope_type은 'ALL' 또는 'GROUPS'이어야 합니다.")
        
        # 사용자 존재 확인
        nurse = self.db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
        if not nurse:
            return {'success': False, 'message': f"간호사/관리자 {nurse_id}를 찾을 수 없습니다."}
        
        # 권한 존재 확인
        permission = self.db.query(Permission).filter(
            Permission.code == permission_code
        ).first()
        if not permission:
            return {'success': False, 'message': f"권한 코드 {permission_code}를 찾을 수 없습니다."}
        
        # 기존 권한 확인 (중복 방지)
        existing = self.db.query(UserPermission).filter(
            and_(
                UserPermission.nurse_id == nurse_id,
                UserPermission.permission_id == permission.permission_id
            )
        ).first()
        
        if existing:
            return {
                'success': False, 
                'message': f"이미 {permission_code} 권한이 부여되어 있습니다. 수정을 원하시면 modify_permission을 사용하세요."
            }
        
        # UserPermission 생성
        user_perm = UserPermission(
            nurse_id=nurse_id,
            permission_id=permission.permission_id,
            scope_type=scope_type,
            granted_by_nurse_id=granted_by_nurse_id,
            granted_at=datetime.now()
        )
        self.db.add(user_perm)
        self.db.flush()  # user_permission_id 생성
        
        # 스코프 생성 (GROUPS인 경우)
        if scope_type == 'GROUPS' and group_ids:
            for gid in group_ids:
                scope = UserPermissionScope(
                    user_permission_id=user_perm.user_permission_id,
                    group_id=gid
                )
                self.db.add(scope)
        
        # 감사 로그 생성
        audit_log = PermissionAuditLog(
            nurse_id=nurse_id,
            permission_code=permission_code,
            action_type='GRANT',
            scope_type=scope_type,
            scope_details=str(group_ids) if group_ids else None,
            performed_by_nurse_id=granted_by_nurse_id or 'SYSTEM',
            performed_at=datetime.now(),
            ip_address=ip_address,
            user_agent=user_agent,
            remarks=f"{permission_code} 권한 부여"
        )
        self.db.add(audit_log)
        
        self.db.commit()
        
        return {
            'success': True, 
            'message': '권한이 성공적으로 부여되었습니다.',
            'user_permission_id': user_perm.user_permission_id
        }
    
    def revoke_permission(
        self,
        nurse_id: str,
        permission_code: str,
        revoked_by_nurse_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        간호사/관리자의 권한 회수
        
        Args:
            nurse_id: 대상 간호사/관리자 ID
            permission_code: 권한 코드
            revoked_by_nurse_id: 회수 수행자 nurse_id
            ip_address: 수행자 IP 주소
            user_agent: 수행자 User Agent
        
        Returns:
            {'success': bool, 'message': str}
        
        Examples:
            >>> manager.revoke_permission('N002', 'ROSTER_CREATE', revoked_by_nurse_id='N001')
            {'success': True, 'message': '권한이 회수되었습니다.'}
        """
        permission = self.db.query(Permission).filter(
            Permission.code == permission_code
        ).first()
        if not permission:
            return {'success': False, 'message': f"권한 코드 {permission_code}를 찾을 수 없습니다."}
        
        user_perm = self.db.query(UserPermission).filter(
            and_(
                UserPermission.nurse_id == nurse_id,
                UserPermission.permission_id == permission.permission_id
            )
        ).first()
        
        if not user_perm:
            return {'success': False, 'message': f"간호사/관리자 {nurse_id}에게 {permission_code} 권한이 없습니다."}
        
        # 감사 로그 생성 (삭제 전에)
        scope_details = None
        if user_perm.scope_type == 'GROUPS':
            scopes = [s.group_id for s in user_perm.scopes]
            scope_details = str(scopes)
        
        audit_log = PermissionAuditLog(
            nurse_id=nurse_id,
            permission_code=permission_code,
            action_type='REVOKE',
            scope_type=user_perm.scope_type,
            scope_details=scope_details,
            performed_by_nurse_id=revoked_by_nurse_id or 'SYSTEM',
            performed_at=datetime.now(),
            ip_address=ip_address,
            user_agent=user_agent,
            remarks=f"{permission_code} 권한 회수"
        )
        self.db.add(audit_log)
        
        # 삭제 (cascade로 UserPermissionScope도 자동 삭제됨)
        self.db.delete(user_perm)
        self.db.commit()
        
        return {'success': True, 'message': '권한이 성공적으로 회수되었습니다.'}
    
    def modify_permission_scope(
        self,
        nurse_id: str,
        permission_code: str,
        new_scope_type: str,
        new_group_ids: Optional[List[str]] = None,
        modified_by_nurse_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        간호사/관리자 권한의 스코프 수정
        
        Args:
            nurse_id: 대상 간호사/관리자 ID
            permission_code: 권한 코드
            new_scope_type: 새로운 스코프 타입
            new_group_ids: 새로운 병동 ID 리스트
            modified_by_nurse_id: 수정 수행자 nurse_id
            ip_address: 수행자 IP 주소
            user_agent: 수행자 User Agent
        
        Returns:
            {'success': bool, 'message': str}
        
        Examples:
            >>> manager.modify_permission_scope(
            ...     nurse_id='N002',
            ...     permission_code='NURSE_UPDATE',
            ...     new_scope_type='ALL',
            ...     modified_by_nurse_id='N001'
            ... )
            {'success': True, 'message': '권한 스코프가 수정되었습니다.'}
        """
        if new_scope_type == 'GROUPS' and (not new_group_ids or len(new_group_ids) == 0):
            raise ValueError("scope_type이 'GROUPS'인 경우 group_ids가 필요합니다.")
        
        permission = self.db.query(Permission).filter(
            Permission.code == permission_code
        ).first()
        if not permission:
            return {'success': False, 'message': f"권한 코드 {permission_code}를 찾을 수 없습니다."}
        
        user_perm = self.db.query(UserPermission).filter(
            and_(
                UserPermission.nurse_id == nurse_id,
                UserPermission.permission_id == permission.permission_id
            )
        ).first()
        
        if not user_perm:
            return {'success': False, 'message': f"간호사/관리자 {nurse_id}에게 {permission_code} 권한이 없습니다."}
        
        # 기존 스코프 정보 백업 (로그용)
        old_scope_type = user_perm.scope_type
        old_scopes = [s.group_id for s in user_perm.scopes] if user_perm.scope_type == 'GROUPS' else None
        
        # 기존 스코프 삭제
        for scope in user_perm.scopes:
            self.db.delete(scope)
        
        # 스코프 타입 변경
        user_perm.scope_type = new_scope_type
        
        # 새 스코프 생성
        if new_scope_type == 'GROUPS' and new_group_ids:
            for gid in new_group_ids:
                scope = UserPermissionScope(
                    user_permission_id=user_perm.user_permission_id,
                    group_id=gid
                )
                self.db.add(scope)
        
        # 감사 로그
        audit_log = PermissionAuditLog(
            nurse_id=nurse_id,
            permission_code=permission_code,
            action_type='MODIFY',
            scope_type=new_scope_type,
            scope_details=f"OLD: {old_scope_type} {old_scopes} → NEW: {new_scope_type} {new_group_ids}",
            performed_by_nurse_id=modified_by_nurse_id or 'SYSTEM',
            performed_at=datetime.now(),
            ip_address=ip_address,
            user_agent=user_agent,
            remarks=f"{permission_code} 권한 스코프 수정"
        )
        self.db.add(audit_log)
        
        self.db.commit()
        
        return {'success': True, 'message': '권한 스코프가 성공적으로 수정되었습니다.'}


def init_master_admin(
    db: Session, 
    nurse_id: str,
    account_id: str, 
    password_hash: str, 
    name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None
) -> Nurse:
    """
    마스터 관리자 계정 초기화
    
    시스템 최초 설정 시 마스터 관리자 계정을 nurses 테이블에 생성하고 모든 권한 부여.
    
    Args:
        db: SQLAlchemy 세션
        nurse_id: 간호사/관리자 ID (예: 'MASTER_001')
        account_id: 마스터 계정 ID
        password_hash: 비밀번호 해시
        name: 관리자 이름
        email: 이메일 주소
        phone: 연락처
    
    Returns:
        생성된 Nurse 객체 (마스터 관리자)
    
    Examples:
        >>> from werkzeug.security import generate_password_hash
        >>> master = init_master_admin(
        ...     db=db,
        ...     nurse_id='MASTER_001',
        ...     account_id='master',
        ...     password_hash=generate_password_hash('secure_password'),
        ...     name='마스터 관리자',
        ...     email='master@hospital.com'
        ... )
    """
    # 마스터 사용자 생성 (Nurse 테이블에)
    master_user = Nurse(
        nurse_id=nurse_id,
        account_id=account_id,
        password_hash=password_hash,
        name=name,
        email=email,
        phone=phone,
        user_type='MASTER',
        is_master=True,
        active=1,  # 활성화
        group_id=None,  # 병동 소속 없음
        experience=None,
        role=None,
        level_=None,
        is_head_nurse=False,
        is_night_nurse=False
    )
    db.add(master_user)
    db.flush()
    
    # 모든 권한 부여 (ALL 스코프)
    permissions = db.query(Permission).filter(Permission.is_active == True).all()
    
    for perm in permissions:
        user_perm = UserPermission(
            nurse_id=master_user.nurse_id,
            permission_id=perm.permission_id,
            scope_type='ALL',
            granted_by_nurse_id='SYSTEM',
            granted_at=datetime.now()
        )
        db.add(user_perm)
    
    db.commit()
    db.refresh(master_user)
    
    return master_user


def init_default_permissions(db: Session) -> List[Permission]:
    """
    기본 권한 코드 초기화
    
    시스템에서 사용할 기본 권한들을 Permission 테이블에 삽입.
    
    Args:
        db: SQLAlchemy 세션
    
    Returns:
        생성된 Permission 객체 리스트
    
    Examples:
        >>> perms = init_default_permissions(db)
        >>> len(perms)
        11
    """
    default_permissions = [
        # NURSE 리소스
        {'code': 'NURSE_READ', 'resource': 'NURSE', 'action': 'READ', 'description': '근무자 조회'},
        {'code': 'NURSE_CREATE', 'resource': 'NURSE', 'action': 'CREATE', 'description': '근무자 생성'},
        {'code': 'NURSE_UPDATE', 'resource': 'NURSE', 'action': 'UPDATE', 'description': '근무자 수정'},
        {'code': 'NURSE_DELETE', 'resource': 'NURSE', 'action': 'DELETE', 'description': '근무자 삭제'},
        
        # ROSTER 리소스
        {'code': 'ROSTER_READ', 'resource': 'ROSTER', 'action': 'READ', 'description': '근무표 조회'},
        {'code': 'ROSTER_CREATE', 'resource': 'ROSTER', 'action': 'CREATE', 'description': '근무표 생성'},
        {'code': 'ROSTER_UPDATE', 'resource': 'ROSTER', 'action': 'UPDATE', 'description': '근무표 수정'},
        {'code': 'ROSTER_DELETE', 'resource': 'ROSTER', 'action': 'DELETE', 'description': '근무표 삭제'},
        
        # PREFERENCE 리소스
        {'code': 'PREFERENCE_READ', 'resource': 'PREFERENCE', 'action': 'READ', 'description': '선호도 조회'},
        
        # ADMIN 리소스 (권한 관리)
        {'code': 'ADMIN_MANAGE_USERS', 'resource': 'ADMIN', 'action': 'MANAGE', 'description': '관리자 계정 관리'},
        {'code': 'ADMIN_MANAGE_PERMISSIONS', 'resource': 'ADMIN', 'action': 'MANAGE', 'description': '권한 관리'},
    ]
    
    created_perms = []
    for perm_data in default_permissions:
        # 중복 체크
        existing = db.query(Permission).filter(Permission.code == perm_data['code']).first()
        if existing:
            created_perms.append(existing)
            continue
        
        perm = Permission(**perm_data)
        db.add(perm)
        created_perms.append(perm)
    
    db.commit()
    
    return created_perms

