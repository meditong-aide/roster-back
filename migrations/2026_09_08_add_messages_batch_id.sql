/* Migration 2026-09-08 — messages.batch_id (+ 조회 인덱스 3종)
 *
 * 보낸 메시지를 **발송 단위**로 묶어 보여주기 위한 식별자.
 * 22명에게 한 번 보내면 보낸함에 카드 1개 · recipient_count=22 · total 1건이 된다.
 * 값은 그 발송 **첫 행의 id** 다(app/services/message_service.py create_message).
 *
 * ★★ **이 마이그레이션은 코드 배포보다 먼저 실행해야 한다.**
 *   batch_id 는 ORM 에 매핑돼 있다(db/models.py Message). SQLAlchemy 는 매핑된
 *   컬럼을 전부 SELECT 하므로, 컬럼이 없으면 신규 엔드포인트뿐 아니라
 *   **기존 메시지 조회·발송·삭제까지 통째로** invalid-column 으로 죽는다.
 *
 * ★ 접속한 DB 에 그대로 적용된다. `USE` 를 쓰지 않는다 —
 *   하드코딩하면 `sqlcmd -d eun_roster_dev` 로 dev 에 넣으려 해도 운영으로 간다.
 *   실행 대상은 **연결 문자열/`-d` 로 지정**할 것.
 *     dev  : sqlcmd -S <host> -d eun_roster_dev -i <이 파일>
 *     prod : sqlcmd -S <host> -d eun_roster     -i <이 파일>
 *   prod→dev 마이그레이션은 DDL 을 옮기지 않으므로 **양쪽에 각각** 돌린다.
 *
 * ★ nullable 로 둔다. 도입 전 행이 이미 있고, 그 행들은 아래 백필로 `id` 를 받는다.
 *   읽는 쪽도 `COALESCE(batch_id, id)` 라 백필 전에도 동작한다(각각 단건 묶음).
 *   NOT NULL 로 조이지 않는 이유는 그 방어가 이미 코드에 있고, 조이면 도입 순서가
 *   백필에 묶여 배포가 더 까다로워지기 때문이다.
 *
 * ★ 인덱스 3종을 함께 넣는다. 도입 시점 messages 는 수 건이라 지금이 가장 싸다.
 *   모바일이 화면 진입마다 받은함·보낸함을 부르므로, 없으면 행이 쌓일수록
 *   매 조회가 테이블 스캔이 된다(도입 전 인덱스는 PK 하나뿐이었다).
 *   컬럼 순서는 실제 조회와 맞춰 뒀다 —
 *     받은함 : WHERE receiver_nurse_id = ? ORDER BY created_at DESC, id DESC
 *     보낸함 : WHERE sender_nurse_id  = ? GROUP BY batch_id ORDER BY batch_id DESC
 *
 * 적용 이력: eun_roster_dev 2026-09-08 / eun_roster 2026-09-08
 */

/* ★ 존재 검사에 TABLE_SCHEMA 를 건다. ALTER 는 dbo 로 고정인데 검사만 스키마를 안 보면,
     같은 이름의 테이블이 다른 스키마에 있을 때 "이미 있다" 로 읽고 dbo 에는 컬럼을 안 넣는다.
     그러면 ORM 이 매핑한 컬럼이 없는 채로 코드가 올라가 모든 Message SELECT 가 죽는다. */
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo'
       AND TABLE_NAME = 'messages'
       AND COLUMN_NAME = 'batch_id'
)
BEGIN
    ALTER TABLE dbo.messages ADD batch_id INT NULL;
    PRINT '[added] dbo.messages.batch_id';
END
ELSE
    PRINT '[skip] batch_id 이미 존재';
GO

/* 백필 — 도입 전 행은 자기 id 를 묶음 키로 삼아 각각 단건이 된다.
   ★ 시각으로 묶지 않는다. created_at 이 datetime(scale 3)이라 ~3.33ms 로 잘려,
     서로 다른 발송이 같은 값을 가질 수 있다. 과거 데이터를 시각으로 묶으면
     남남인 발송이 한 카드로 합쳐진다. */
UPDATE dbo.messages SET batch_id = id WHERE batch_id IS NULL;
PRINT '[backfilled] batch_id = id (도입 전 행)';
GO

/* 조회 인덱스 — 없으면 받은함·보낸함이 매번 테이블 스캔이다. */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
                WHERE object_id = OBJECT_ID('dbo.messages') AND name = 'IX_messages_receiver')
BEGIN
    CREATE INDEX IX_messages_receiver ON dbo.messages
        (receiver_nurse_id, created_at DESC, id DESC);
    PRINT '[added] IX_messages_receiver';
END
ELSE
    PRINT '[skip] IX_messages_receiver 이미 존재';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
                WHERE object_id = OBJECT_ID('dbo.messages') AND name = 'IX_messages_sender')
BEGIN
    CREATE INDEX IX_messages_sender ON dbo.messages
        (sender_nurse_id, created_at DESC, id);
    PRINT '[added] IX_messages_sender';
END
ELSE
    PRINT '[skip] IX_messages_sender 이미 존재';
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
                WHERE object_id = OBJECT_ID('dbo.messages') AND name = 'IX_messages_sender_batch')
BEGIN
    CREATE INDEX IX_messages_sender_batch ON dbo.messages
        (sender_nurse_id, batch_id DESC);
    PRINT '[added] IX_messages_sender_batch';
END
ELSE
    PRINT '[skip] IX_messages_sender_batch 이미 존재';
GO

/* 검증 — 접속 DB 기준. 위 DDL 과 같은 스키마(dbo)를 봐야 검증이 성립한다. */
SELECT DB_NAME() AS applied_to, TABLE_SCHEMA, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'messages' AND COLUMN_NAME = 'batch_id';
GO

SELECT DB_NAME() AS applied_to, name, type_desc
  FROM sys.indexes
 WHERE object_id = OBJECT_ID('dbo.messages') AND name LIKE 'IX_messages%';
GO

/* 백필 누락이 없는지 — 0 이어야 한다. */
SELECT COUNT(*) AS batch_id_null_left FROM dbo.messages WHERE batch_id IS NULL;
GO

/* 롤백
     DROP INDEX IX_messages_sender_batch ON dbo.messages;
     DROP INDEX IX_messages_sender       ON dbo.messages;
     DROP INDEX IX_messages_receiver     ON dbo.messages;
     ALTER TABLE dbo.messages DROP COLUMN batch_id;
   ★ ORM 매핑(db/models.py Message.batch_id)을 **먼저** 되돌린 뒤에 할 것.
     순서가 뒤바뀌면 그 사이 모든 메시지 조회가 실패한다.
*/
