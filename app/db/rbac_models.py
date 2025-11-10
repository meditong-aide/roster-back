"""
RBAC-Lite 권한 관리 모델

Role 테이블 없이 Nurse → Permission → Scope를 직접 연결하는 경량 권한 시스템.
리소스별 CRUD 권한을 개별 부여하며, 스코프는 여러 병동 또는 전체로 지정 가능.

Note: AdminUser 테이블은 제거하고 기존 Nurse 테이블을 확장하여 사용합니다.
      Nurse 테이블에 password_hash, user_type, is_master 등 관리자 관련 컬럼이 추가되었습니다.
"""

from sqlalchemy import (
    Column, VARCHAR, INTEGER, BOOLEAN, DATETIME, 
    ForeignKey, TEXT, Index, func, UniqueConstraint
)
from sqlalchemy.orm import relationship
from db.client2 import Base


class Permission(Base):
    """
    권한 정의 마스터 테이블
    
    시스템에서 사용 가능한 모든 권한 코드를 정의.
    리소스별 CRUD 액션으로 구성.
    
    Attributes:
        permission_id: 권한 고유 ID (PK, AUTO_INCREMENT)
        code: 권한 코드 (예: NURSE_READ, ROSTER_CREATE) - UNIQUE
        resource: 리소스 타입 (NURSE, ROSTER, PREFERENCE 등)
        action: 액션 타입 (READ, CREATE, UPDATE, DELETE)
        description: 권한 설명
        is_active: 권한 활성화 상태
        created_at: 생성 시간
    
    Examples:
        - code='NURSE_READ', resource='NURSE', action='READ'
        - code='ROSTER_CREATE', resource='ROSTER', action='CREATE'
        - code='PREFERENCE_READ', resource='PREFERENCE', action='READ'
    """
    __tablename__ = 'permissions'
    
    permission_id = Column(INTEGER, primary_key=True, autoincrement=True)
    code = Column(VARCHAR(50), unique=True, nullable=False, index=True)
    resource = Column(VARCHAR(30), nullable=False)
    action = Column(VARCHAR(20), nullable=False)
    description = Column(TEXT, nullable=True)
    is_active = Column(BOOLEAN, nullable=False, default=True)
    created_at = Column(DATETIME, nullable=False, default=func.now())
    
    # Relationships
    user_permissions = relationship("UserPermission", back_populates="permission")
    
    # 복합 인덱스 및 제약조건
    __table_args__ = (
        Index('idx_permission_resource_action', 'resource', 'action'),
        UniqueConstraint('resource', 'action', name='uq_resource_action'),
    )


class UserPermission(Base):
    """
    사용자-권한 매핑 테이블
    
    특정 간호사/관리자에게 부여된 권한과 스코프 정보.
    scope_type이 'ALL'이면 전체 병동, 'GROUPS'이면 특정 병동들에만 적용.
    
    Attributes:
        user_permission_id: 매핑 고유 ID (PK, AUTO_INCREMENT)
        nurse_id: 간호사/관리자 ID (FK → nurses.nurse_id)
        permission_id: 권한 ID (FK)
        scope_type: 스코프 타입 ('ALL' 또는 'GROUPS')
        granted_at: 권한 부여 시간
        granted_by_nurse_id: 권한 부여자 nurse_id
    
    Scope Type:
        - 'ALL': 전체 병동에 대한 권한 (UserPermissionScope 불필요)
        - 'GROUPS': 특정 병동들에 대한 권한 (UserPermissionScope에 병동 목록 저장)
    
    Examples:
        - nurse_id='N001', permission='ROSTER_READ', scope_type='ALL'
          → 모든 병동의 근무표 조회 가능
        - nurse_id='N002', permission='NURSE_UPDATE', scope_type='GROUPS'
          → UserPermissionScope에 지정된 병동들의 근무자만 수정 가능
    """
    __tablename__ = 'user_permissions'
    
    user_permission_id = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id = Column(VARCHAR(50), ForeignKey('nurses.nurse_id'), nullable=False)
    permission_id = Column(INTEGER, ForeignKey('permissions.permission_id'), nullable=False)
    scope_type = Column(VARCHAR(10), nullable=False, default='GROUPS')  # 'ALL' or 'GROUPS'
    granted_at = Column(DATETIME, nullable=False, default=func.now())
    granted_by_nurse_id = Column(VARCHAR(50), ForeignKey('nurses.nurse_id'), nullable=True)
    
    # Relationships
    nurse = relationship("Nurse", back_populates="permissions", foreign_keys=[nurse_id])
    permission = relationship("Permission", back_populates="user_permissions")
    granter = relationship("Nurse", foreign_keys=[granted_by_nurse_id])
    scopes = relationship("UserPermissionScope", back_populates="user_permission", cascade="all, delete-orphan")
    
    # 복합 인덱스 및 제약조건
    __table_args__ = (
        Index('idx_nurse_permission', 'nurse_id', 'permission_id'),
        UniqueConstraint('nurse_id', 'permission_id', name='uq_nurse_permission'),
    )


class UserPermissionScope(Base):
    """
    사용자 권한 스코프 (병동별 권한) 테이블
    
    UserPermission의 scope_type이 'GROUPS'인 경우,
    어떤 병동(group_id)에 대해 권한이 적용되는지 저장.
    
    Attributes:
        scope_id: 스코프 고유 ID (PK, AUTO_INCREMENT)
        user_permission_id: 사용자 권한 매핑 ID (FK)
        group_id: 병동 ID (FK)
        created_at: 생성 시간
    
    Query Pattern:
        # 권한 체크 예시 (user_id='U001', permission='NURSE_UPDATE', group_id='G001')
        1. UserPermission에서 user_id, permission_id로 조회
        2. scope_type이 'ALL'이면 즉시 통과
        3. scope_type이 'GROUPS'이면 UserPermissionScope에서 group_id 존재 여부 확인
    
    Examples:
        - user_permission_id=10, group_id='G001'
        - user_permission_id=10, group_id='G002'
        → user_permission_id=10에 해당하는 권한이 G001, G002 병동에만 적용
    """
    __tablename__ = 'user_permission_scopes'
    
    scope_id = Column(INTEGER, primary_key=True, autoincrement=True)
    user_permission_id = Column(INTEGER, ForeignKey('user_permissions.user_permission_id'), nullable=False)
    group_id = Column(VARCHAR(50), ForeignKey('groups.group_id'), nullable=False)
    created_at = Column(DATETIME, nullable=False, default=func.now())
    
    # Relationships
    user_permission = relationship("UserPermission", back_populates="scopes")
    group = relationship("Group")
    
    # 복합 인덱스 및 제약조건
    __table_args__ = (
        Index('idx_permission_group', 'user_permission_id', 'group_id'),
        UniqueConstraint('user_permission_id', 'group_id', name='uq_permission_scope'),
    )


class PermissionAuditLog(Base):
    """
    권한 변경 감사 로그 테이블
    
    권한 부여/삭제/수정 이력을 추적하여 보안 감사 및 이력 관리.
    
    Attributes:
        log_id: 로그 고유 ID (PK, AUTO_INCREMENT)
        nurse_id: 대상 간호사/관리자 ID
        permission_code: 권한 코드
        action_type: 액션 타입 (GRANT, REVOKE, MODIFY)
        scope_type: 스코프 타입
        scope_details: 스코프 상세 (JSON 형태 - 병동 목록 등)
        performed_by_nurse_id: 수행자 nurse_id
        performed_at: 수행 시간
        ip_address: 수행자 IP 주소
        user_agent: 수행자 User Agent
        remarks: 비고
    
    Examples:
        - nurse_id='N001', action_type='GRANT', permission_code='ROSTER_CREATE'
          → N001에게 ROSTER_CREATE 권한 부여
        - nurse_id='N002', action_type='REVOKE', permission_code='NURSE_DELETE'
          → N002의 NURSE_DELETE 권한 회수
    """
    __tablename__ = 'permission_audit_logs'
    
    log_id = Column(INTEGER, primary_key=True, autoincrement=True)
    nurse_id = Column(VARCHAR(50), nullable=False)
    permission_code = Column(VARCHAR(50), nullable=False)
    action_type = Column(VARCHAR(20), nullable=False)  # GRANT, REVOKE, MODIFY
    scope_type = Column(VARCHAR(10), nullable=True)
    scope_details = Column(TEXT, nullable=True)
    performed_by_nurse_id = Column(VARCHAR(50), nullable=False)
    performed_at = Column(DATETIME, nullable=False, default=func.now())
    ip_address = Column(VARCHAR(45), nullable=True)
    user_agent = Column(TEXT, nullable=True)
    remarks = Column(TEXT, nullable=True)
    
    # 인덱스
    __table_args__ = (
        Index('idx_audit_nurse_time', 'nurse_id', 'performed_at'),
        Index('idx_audit_performer_nurse_time', 'performed_by_nurse_id', 'performed_at'),
    )

