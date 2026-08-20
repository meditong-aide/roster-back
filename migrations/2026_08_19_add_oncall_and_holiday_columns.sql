/* Migration 2026-08-19 — 수술실 콜 당번 + 고정근무자 공휴일 휴무 컬럼 2종.
 *
 * 이미 dev·prod 에 적용돼 있다(2026-08 배포분). 재현용 기록이다.
 *
 * ★ 두 컬럼 모두 **ORM 매핑이 없다.** 코드는 raw SQL 로 읽되 INFORMATION_SCHEMA 로
 *   존재를 먼저 확인하고 없으면 기능을 끈 것으로 떨어뜨린다
 *   (services/oncall_assign.py `load_call_code_map` ·
 *    services/roster_create_service.py `_holiday_off_enabled`).
 *   그래서 **코드를 먼저 배포하고 DDL 을 나중에 넣어도 안전**하고, 반대 순서도 안전하다.
 */

/* ─────────────────────────────────────────────────────────────
 * ① shifts.call_base_id — 콜 코드가 어느 근무코드를 대체하는지
 *
 * 콜은 근무를 추가하는 게 아니라 **셀의 코드를 갈아끼운다**(D1 → D1콜, O → 오프콜).
 * schedule_entries 가 (간호사, 날짜)당 코드 하나만 갖기 때문이다.
 *
 * ★ 값이 들어 있는 것 자체가 "그 그룹이 콜을 쓴다"는 스위치다. 별도 on/off 플래그를
 *   두지 않는다 — 기본값을 두면 모든 병동에 있는 O 가 걸려 미사용 병동까지 콜이 붙는다.
 * ★ shift_id 가 아니라 shifts.id(INT)를 가리킨다. 코드 문자열은 병동마다 갈리지만
 *   id 는 안정적이다(저장소 관행: schedule_entries.id · nurse_shift_requests.shifts_table_id).
 *
 * 등록 예(인천의료원 수술실 102243f1d943):
 *   UPDATE shifts SET call_base_id = 1846 WHERE id = 1867;  -- D1콜  → D1
 *   UPDATE shifts SET call_base_id = 1840 WHERE id = 1868;  -- 오프콜 → O
 *   ★ 오프콜은 type 도 '휴무' 여야 한다 — '근무' 로 두면 연속근무일수가 부풀어
 *     위반 검출이 통째로 어긋난다(_build_validation_shift_main_map).
 * ───────────────────────────────────────────────────────────── */
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'shifts' AND COLUMN_NAME = 'call_base_id'
)
BEGIN
    ALTER TABLE dbo.shifts ADD call_base_id INT NULL;
END;

/* ─────────────────────────────────────────────────────────────
 * ② roster_config.fixed_holiday_off_yn — 고정근무자를 공휴일에 쉬게 할지
 *
 * 고정근무자는 CP-SAT 을 타지 않고 평일=고정코드 / 주말=OFF 로 채워진다. 이 값을 켜면
 * **평일 공휴일도 OFF** 가 된다(python-holidays KR 기준).
 * 실측 2026-08-17 광복절 대체휴일: 13개 병동 고정근무자 28명이 전원 O 였는데
 * 생성기는 공휴일을 몰라 근무로 채웠다 — 그 20셀을 해소한 설정이다.
 *
 * ★ NULL = 미설정(= 꺼짐). 기존 row 를 건드리지 않으려 nullable 로 둔다.
 * ★ 이 컬럼은 `_PRESERVE_IF_NONE`(services/roster_service.py) 목록에 없다.
 *   설정 화면에서 새 프리셋을 저장하면 새 row 가 NULL 로 생기고,
 *   `_holiday_off_enabled` 는 config_id DESC 첫 건을 읽으므로 설정이 유실될 수 있다.
 *   → 켤 때는 그 그룹의 **전 row** 에 넣는 편이 안전하다.
 * ───────────────────────────────────────────────────────────── */
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'fixed_holiday_off_yn'
)
BEGIN
    ALTER TABLE dbo.roster_config ADD fixed_holiday_off_yn BIT NULL;
END;

/* 검증 */
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE (TABLE_NAME = 'shifts' AND COLUMN_NAME = 'call_base_id')
   OR (TABLE_NAME = 'roster_config' AND COLUMN_NAME = 'fixed_holiday_off_yn');
