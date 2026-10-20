-- 공개 R&D 과제 데이터 스키마.
-- 컬럼 이름은 영문, 원본 한글 항목명과 의미는 COMMENT 로 남긴다.
-- 에이전트의 describe_table 도구가 이 COMMENT 를 그대로 읽어 스키마를 이해한다.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- KEIT: 산업통상자원부 산하 한국산업기술기획평가원 지원 과제 (2015~2026)
-- ---------------------------------------------------------------------------
CREATE TABLE keit_projects (
    project_no        bigint PRIMARY KEY,
    support_year      smallint NOT NULL,
    title             text NOT NULL,
    start_date        date,
    end_date          date,
    lead_org          text NOT NULL,
    lead_org_code     text,
    field_detail      text,
    tech_field        text,
    program_category  text,
    sub_program       text NOT NULL,
    ministry          text NOT NULL,
    ministry_code     text NOT NULL
);
COMMENT ON TABLE keit_projects IS
    'KEIT(한국산업기술기획평가원) 산업기술 R&D 지원 과제. 1행 = 1과제. 2015~2026 지원년도. 연구비 금액은 없음. 출처: 공공데이터포털 15018033 (2026-07-21 기준)';
COMMENT ON COLUMN keit_projects.project_no IS '과제번호 (고유)';
COMMENT ON COLUMN keit_projects.support_year IS '지원년도';
COMMENT ON COLUMN keit_projects.title IS '과제명';
COMMENT ON COLUMN keit_projects.start_date IS '총개발기간 시작일';
COMMENT ON COLUMN keit_projects.end_date IS '총개발기간 종료일';
COMMENT ON COLUMN keit_projects.lead_org IS '주관기관명';
COMMENT ON COLUMN keit_projects.lead_org_code IS '주관기관 행정표준기관코드 (공공기관만, 없으면 NULL)';
COMMENT ON COLUMN keit_projects.field_detail IS '세부분야 (예: 자동차/철도차량, 반도체장비). 분류가 없으면 ''미분류''';
COMMENT ON COLUMN keit_projects.tech_field IS '기술분야 (세부분야보다 잘게 나눈 분류). 분류가 없으면 ''미분류''';
COMMENT ON COLUMN keit_projects.program_category IS '사업분류 (예: 산업핵심(일반회계), 소재부품). 분류가 없으면 ''미분류''';
COMMENT ON COLUMN keit_projects.sub_program IS '내역사업명';
COMMENT ON COLUMN keit_projects.ministry IS '주관부처명 (산업통상자원부, 행정안전부, 소방청, 해양경찰청)';
COMMENT ON COLUMN keit_projects.ministry_code IS '주관부처 행정표준기관코드';

CREATE TABLE keit_project_participants (
    project_no  bigint NOT NULL REFERENCES keit_projects (project_no),
    org_name    text NOT NULL,
    PRIMARY KEY (project_no, org_name)
);
COMMENT ON TABLE keit_project_participants IS
    'KEIT 과제의 참여기관(주관기관 제외). 1행 = 과제×참여기관. 원본 "참여기관" 칸의 쉼표 목록을 나눈 것';
COMMENT ON COLUMN keit_project_participants.project_no IS '과제번호 (keit_projects.project_no)';
COMMENT ON COLUMN keit_project_participants.org_name IS '참여기관명';

-- ---------------------------------------------------------------------------
-- KIMST: 해양수산과학기술진흥원 해양수산 R&D 과제별·기관별 연구비 (2021~2023)
-- ---------------------------------------------------------------------------
CREATE TABLE kimst_project_budgets (
    id                serial PRIMARY KEY,
    budget_year       smallint NOT NULL,
    program_name      text NOT NULL,
    project_title     text NOT NULL,
    role              text NOT NULL,
    org_name          text NOT NULL,
    org_type          text,
    start_date        date,
    end_date          date,
    total_budget_krw  bigint NOT NULL,
    gov_grant_krw     bigint NOT NULL
);
COMMENT ON TABLE kimst_project_budgets IS
    'KIMST(해양수산과학기술진흥원) 해양수산 R&D 과제의 기관별 당해년도 연구비. 1행 = 예산년도×과제×수행기관. 과제 고유번호가 없으므로 과제는 (budget_year, program_name, project_title) 로 구분. 2023년 이후 IRIS 로 이관되어 갱신 중단. 출처: 공공데이터포털 15104466 (2023-05-04 기준)';
COMMENT ON COLUMN kimst_project_budgets.budget_year IS '예산년도';
COMMENT ON COLUMN kimst_project_budgets.program_name IS '사업명';
COMMENT ON COLUMN kimst_project_budgets.project_title IS '과제명';
COMMENT ON COLUMN kimst_project_budgets.role IS '구분: 세부(과제 주관), 공동(공동연구기관), 위탁(위탁연구기관)';
COMMENT ON COLUMN kimst_project_budgets.org_name IS '연구기관명';
COMMENT ON COLUMN kimst_project_budgets.org_type IS '기관유형 (대학, 중소기업, 출연연구소 등)';
COMMENT ON COLUMN kimst_project_budgets.start_date IS '당해년도 연구기간 시작일';
COMMENT ON COLUMN kimst_project_budgets.end_date IS '당해년도 연구기간 종료일';
COMMENT ON COLUMN kimst_project_budgets.total_budget_krw IS '총 연구비 (원). 해당 기관 몫';
COMMENT ON COLUMN kimst_project_budgets.gov_grant_krw IS '정부출연금 (원). 해당 기관 몫, 총 연구비 이하';

-- ---------------------------------------------------------------------------
-- KETEP: 한국에너지기술평가원 지원 과제별 수행기관 (2022)
-- ---------------------------------------------------------------------------
CREATE TABLE ketep_project_orgs (
    id              serial PRIMARY KEY,
    base_year       smallint NOT NULL,
    detail_program  text NOT NULL,
    sub_program     text NOT NULL,
    account_name    text NOT NULL,
    project_title   text NOT NULL,
    org_role        text NOT NULL,
    org_name        text NOT NULL,
    org_type        text,
    org_region      text
);
COMMENT ON TABLE ketep_project_orgs IS
    'KETEP(한국에너지기술평가원) 에너지 R&D 지원 과제의 수행기관 목록. 1행 = 과제×수행기관. 2022 기준연도만 있음. 연구비 금액은 없음. 과제 고유번호가 없으므로 과제는 project_title 로 구분. 출처: 공공데이터포털 15106650 (2022-12-31 기준)';
COMMENT ON COLUMN ketep_project_orgs.base_year IS '기준연도 (과제 수행 연도)';
COMMENT ON COLUMN ketep_project_orgs.detail_program IS '세부사업명 (내역사업의 상위)';
COMMENT ON COLUMN ketep_project_orgs.sub_program IS '내역사업명';
COMMENT ON COLUMN ketep_project_orgs.account_name IS '회계명: 정부 연구비 재원 (전력기금=전력산업기반기금, 에특=에너지및자원사업특별회계, 기후기금=기후대응기금, 방폐기금=방사성폐기물관리기금, 산촉기금=산업기술진흥및사업화촉진기금, 일반=일반회계)';
COMMENT ON COLUMN ketep_project_orgs.project_title IS '과제명';
COMMENT ON COLUMN ketep_project_orgs.org_role IS '수행기관 역할: 주관연구개발기관 또는 공동연구개발기관';
COMMENT ON COLUMN ketep_project_orgs.org_name IS '수행기관명';
COMMENT ON COLUMN ketep_project_orgs.org_type IS '수행기관 유형 (중소기업, 대학, 출연연구소, 대기업, 공기업 등)';
COMMENT ON COLUMN ketep_project_orgs.org_region IS '수행기관 소재 시도';
