-- ESG-lite database schema as the Node backend's TypeORM entities define it
-- (redesign/integration, 0371455). This service reads and writes those
-- tables, so CI runs its SQL against this copy. Regenerate after an ESG-lite schema change:
--   pg_dump --schema-only --no-owner --no-privileges <ci database built by ESG-lite ci/schema-sync.cjs>
CREATE TYPE public.emission_document_document_type_enum AS ENUM (
    'invoice',
    'receipt',
    'report',
    'certificate',
    'other'
);
CREATE TYPE public.emission_status_enum AS ENUM (
    'pending',
    'approved',
    'rejected'
);
CREATE TYPE public.production_data_status_enum AS ENUM (
    'pending',
    'approved',
    'rejected'
);
CREATE TABLE public.audit_log (
    id integer NOT NULL,
    entity_type character varying NOT NULL,
    entity_id integer NOT NULL,
    action character varying NOT NULL,
    changed_fields jsonb NOT NULL,
    reason text,
    changed_at timestamp without time zone DEFAULT now() NOT NULL,
    changed_by integer
);
CREATE SEQUENCE public.audit_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;
CREATE TABLE public.brand (
    company_id integer NOT NULL,
    name character varying NOT NULL,
    "primary" character varying DEFAULT '#1f2a44'::character varying NOT NULL,
    accent character varying DEFAULT '#3b82f6'::character varying NOT NULL,
    cover_from character varying DEFAULT '#0d1526'::character varying NOT NULL,
    cover_to character varying DEFAULT '#1f2a44'::character varying NOT NULL,
    logo_url character varying,
    logo_public_id character varying,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.category (
    category_id integer NOT NULL,
    category_name character varying NOT NULL,
    scope character varying
);
CREATE SEQUENCE public.category_category_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.category_category_id_seq OWNED BY public.category.category_id;
CREATE TABLE public.column_config (
    pk_id integer NOT NULL,
    config_name character varying NOT NULL,
    column_options jsonb DEFAULT '{}'::jsonb,
    column_dependencies jsonb DEFAULT '{}'::jsonb,
    dependent_options jsonb DEFAULT '{}'::jsonb,
    emission_category_mapping jsonb DEFAULT '{}'::jsonb,
    extra_fields jsonb DEFAULT '[]'::jsonb,
    calculation jsonb,
    site_id integer,
    category_id integer
);
CREATE TABLE public.column_config_columns (
    column_config_id integer NOT NULL,
    column_id integer NOT NULL
);
CREATE SEQUENCE public.column_config_pk_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.column_config_pk_id_seq OWNED BY public.column_config.pk_id;
CREATE TABLE public.column_entity (
    pk_id integer NOT NULL,
    column_name character varying NOT NULL,
    column_type character varying NOT NULL
);
CREATE SEQUENCE public.column_entity_pk_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.column_entity_pk_id_seq OWNED BY public.column_entity.pk_id;
CREATE TABLE public.company (
    company_id integer NOT NULL,
    name character varying NOT NULL,
    address character varying NOT NULL,
    contact_person character varying NOT NULL,
    email character varying,
    phone_number character varying,
    industry character varying,
    region character varying,
    employee_range character varying,
    cin_number character varying,
    status boolean DEFAULT true NOT NULL,
    subscription_id character varying,
    is_email_verified boolean DEFAULT false NOT NULL,
    esg_mitra_access boolean DEFAULT false NOT NULL
);
CREATE SEQUENCE public.company_company_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.company_company_id_seq OWNED BY public.company.company_id;
CREATE TABLE public.country (
    country_id integer NOT NULL,
    name character varying NOT NULL,
    code character varying NOT NULL
);
CREATE SEQUENCE public.country_country_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.country_country_id_seq OWNED BY public.country.country_id;
CREATE TABLE public.emission (
    pk_id integer NOT NULL,
    activity_data jsonb NOT NULL,
    total_emission numeric NOT NULL,
    unit character varying NOT NULL,
    date_of_reporting date NOT NULL,
    activity_data_unit character varying,
    status public.emission_status_enum DEFAULT 'pending'::public.emission_status_enum NOT NULL,
    review_comment character varying,
    reviewed_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    reporting_period character varying(10) DEFAULT 'monthly'::character varying NOT NULL,
    year_type character varying(2),
    fera_linked_id integer,
    upload_batch_id character varying,
    extra_data jsonb DEFAULT '{}'::jsonb,
    emission_factor_snapshot jsonb,
    reviewed_by integer,
    created_by integer,
    category_id integer,
    site_id integer
);
CREATE TABLE public.emission_category_mapping (
    id integer NOT NULL,
    company_id integer NOT NULL,
    company_name character varying NOT NULL,
    site_id integer,
    category_id integer NOT NULL,
    company_category_name character varying NOT NULL,
    global_category_name character varying NOT NULL,
    emission_factor_id integer,
    created_by integer,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.emission_category_mapping_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.emission_category_mapping_id_seq OWNED BY public.emission_category_mapping.id;
CREATE TABLE public.emission_document (
    document_id integer NOT NULL,
    file_name character varying NOT NULL,
    original_name character varying NOT NULL,
    cloudinary_public_id character varying NOT NULL,
    cloudinary_url character varying NOT NULL,
    secure_url character varying,
    file_type character varying NOT NULL,
    file_size integer,
    document_type public.emission_document_document_type_enum DEFAULT 'other'::public.emission_document_document_type_enum NOT NULL,
    description character varying,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    emission_id integer,
    uploaded_by integer
);
CREATE SEQUENCE public.emission_document_document_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.emission_document_document_id_seq OWNED BY public.emission_document.document_id;
CREATE TABLE public.emission_factors (
    emission_factor_id integer NOT NULL,
    year integer NOT NULL,
    factor_value numeric(10,4) NOT NULL,
    denominator_unit character varying,
    source character varying,
    emission_category_name character varying,
    global_category_name character varying,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    upload_batch_id character varying,
    site_id integer,
    category_id integer
);
CREATE SEQUENCE public.emission_factors_emission_factor_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.emission_factors_emission_factor_id_seq OWNED BY public.emission_factors.emission_factor_id;
CREATE SEQUENCE public.emission_pk_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.emission_pk_id_seq OWNED BY public.emission.pk_id;
CREATE TABLE public.emission_threshold (
    threshold_id integer NOT NULL,
    threshold_percentage numeric(5,2) DEFAULT '5'::numeric NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    company_id integer
);
CREATE SEQUENCE public.emission_threshold_threshold_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.emission_threshold_threshold_id_seq OWNED BY public.emission_threshold.threshold_id;
CREATE TABLE public.notification (
    id integer NOT NULL,
    type character varying NOT NULL,
    title character varying NOT NULL,
    message text NOT NULL,
    link character varying,
    read boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    user_id integer
);
CREATE SEQUENCE public.notification_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.notification_id_seq OWNED BY public.notification.id;
CREATE TABLE public.product (
    product_id integer NOT NULL,
    name character varying NOT NULL,
    description character varying,
    unit character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    site_id integer
);
CREATE SEQUENCE public.product_product_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.product_product_id_seq OWNED BY public.product.product_id;
CREATE TABLE public.production_data (
    production_id integer NOT NULL,
    quantity numeric(15,4) NOT NULL,
    unit character varying NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    notes character varying,
    status public.production_data_status_enum DEFAULT 'pending'::public.production_data_status_enum NOT NULL,
    review_comment character varying,
    reviewed_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    product_id integer,
    site_id integer,
    reviewed_by integer,
    created_by integer
);
CREATE SEQUENCE public.production_data_production_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.production_data_production_id_seq OWNED BY public.production_data.production_id;
CREATE TABLE public.site (
    site_id integer NOT NULL,
    name character varying NOT NULL,
    address character varying NOT NULL,
    contact_person character varying NOT NULL,
    company_id integer,
    country_id integer
);
CREATE TABLE public.site_categories (
    site_id integer NOT NULL,
    category_id integer NOT NULL
);
CREATE SEQUENCE public.site_site_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.site_site_id_seq OWNED BY public.site.site_id;
CREATE TABLE public.unit (
    unit_id integer NOT NULL,
    unit_name character varying NOT NULL,
    description character varying,
    site_id integer,
    category_id integer
);
CREATE SEQUENCE public.unit_unit_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.unit_unit_id_seq OWNED BY public.unit.unit_id;
CREATE TABLE public."user" (
    user_id integer NOT NULL,
    name character varying,
    last_name character varying,
    phone_number character varying,
    email character varying NOT NULL,
    password character varying NOT NULL,
    password_reset_token character varying,
    password_reset_expires timestamp without time zone,
    role character varying NOT NULL,
    notification_preferences jsonb DEFAULT '{}'::jsonb,
    timezone character varying,
    site_id integer
);
CREATE TABLE public.user_categories (
    user_id integer NOT NULL,
    category_id integer NOT NULL
);
CREATE TABLE public.user_sites (
    user_id integer NOT NULL,
    site_id integer NOT NULL
);
CREATE SEQUENCE public.user_user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.user_user_id_seq OWNED BY public."user".user_id;
ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);
ALTER TABLE ONLY public.category ALTER COLUMN category_id SET DEFAULT nextval('public.category_category_id_seq'::regclass);
ALTER TABLE ONLY public.column_config ALTER COLUMN pk_id SET DEFAULT nextval('public.column_config_pk_id_seq'::regclass);
ALTER TABLE ONLY public.column_entity ALTER COLUMN pk_id SET DEFAULT nextval('public.column_entity_pk_id_seq'::regclass);
ALTER TABLE ONLY public.company ALTER COLUMN company_id SET DEFAULT nextval('public.company_company_id_seq'::regclass);
ALTER TABLE ONLY public.country ALTER COLUMN country_id SET DEFAULT nextval('public.country_country_id_seq'::regclass);
ALTER TABLE ONLY public.emission ALTER COLUMN pk_id SET DEFAULT nextval('public.emission_pk_id_seq'::regclass);
ALTER TABLE ONLY public.emission_category_mapping ALTER COLUMN id SET DEFAULT nextval('public.emission_category_mapping_id_seq'::regclass);
ALTER TABLE ONLY public.emission_document ALTER COLUMN document_id SET DEFAULT nextval('public.emission_document_document_id_seq'::regclass);
ALTER TABLE ONLY public.emission_factors ALTER COLUMN emission_factor_id SET DEFAULT nextval('public.emission_factors_emission_factor_id_seq'::regclass);
ALTER TABLE ONLY public.emission_threshold ALTER COLUMN threshold_id SET DEFAULT nextval('public.emission_threshold_threshold_id_seq'::regclass);
ALTER TABLE ONLY public.notification ALTER COLUMN id SET DEFAULT nextval('public.notification_id_seq'::regclass);
ALTER TABLE ONLY public.product ALTER COLUMN product_id SET DEFAULT nextval('public.product_product_id_seq'::regclass);
ALTER TABLE ONLY public.production_data ALTER COLUMN production_id SET DEFAULT nextval('public.production_data_production_id_seq'::regclass);
ALTER TABLE ONLY public.site ALTER COLUMN site_id SET DEFAULT nextval('public.site_site_id_seq'::regclass);
ALTER TABLE ONLY public.unit ALTER COLUMN unit_id SET DEFAULT nextval('public.unit_unit_id_seq'::regclass);
ALTER TABLE ONLY public."user" ALTER COLUMN user_id SET DEFAULT nextval('public.user_user_id_seq'::regclass);
ALTER TABLE ONLY public.site_categories
    ADD CONSTRAINT "PK_07998a7adf9d35c123316fc14d3" PRIMARY KEY (site_id, category_id);
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT "PK_07fefa57f7f5ab8fc3f52b3ed0b" PRIMARY KEY (id);
ALTER TABLE ONLY public.column_config
    ADD CONSTRAINT "PK_189b358918c2e655a73e145355e" PRIMARY KEY (pk_id);
ALTER TABLE ONLY public.product
    ADD CONSTRAINT "PK_1de6a4421ff0c410d75af27aeee" PRIMARY KEY (product_id);
ALTER TABLE ONLY public.country
    ADD CONSTRAINT "PK_220fe368500f103cf873b01f159" PRIMARY KEY (country_id);
ALTER TABLE ONLY public.column_entity
    ADD CONSTRAINT "PK_24e4803a93f72f4bac2ffedb47c" PRIMARY KEY (pk_id);
ALTER TABLE ONLY public.column_config_columns
    ADD CONSTRAINT "PK_4ffd4c37a1469fc73b970feb57c" PRIMARY KEY (column_config_id, column_id);
ALTER TABLE ONLY public.site
    ADD CONSTRAINT "PK_5a3b28c4439c0972c59a0504e6b" PRIMARY KEY (site_id);
ALTER TABLE ONLY public.notification
    ADD CONSTRAINT "PK_705b6c7cdf9b2c2ff7ac7872cb7" PRIMARY KEY (id);
ALTER TABLE ONLY public."user"
    ADD CONSTRAINT "PK_758b8ce7c18b9d347461b30228d" PRIMARY KEY (user_id);
ALTER TABLE ONLY public.emission_threshold
    ADD CONSTRAINT "PK_7ba0108007d786640d51f6992da" PRIMARY KEY (threshold_id);
ALTER TABLE ONLY public.user_categories
    ADD CONSTRAINT "PK_802a1731c44150d3e7e30ea75c2" PRIMARY KEY (user_id, category_id);
ALTER TABLE ONLY public.production_data
    ADD CONSTRAINT "PK_8221736c70050f9c6891a5a45e5" PRIMARY KEY (production_id);
ALTER TABLE ONLY public.emission
    ADD CONSTRAINT "PK_8451c15f32af447f44f82e87b0c" PRIMARY KEY (pk_id);
ALTER TABLE ONLY public.unit
    ADD CONSTRAINT "PK_8893a61126ad0507e5d6a63ecb3" PRIMARY KEY (unit_id);
ALTER TABLE ONLY public.brand
    ADD CONSTRAINT "PK_989bbcbbbd77be6eeba9167e73b" PRIMARY KEY (company_id);
ALTER TABLE ONLY public.emission_factors
    ADD CONSTRAINT "PK_a4964fdc8e03b6b67dc89375e5b" PRIMARY KEY (emission_factor_id);
ALTER TABLE ONLY public.company
    ADD CONSTRAINT "PK_b7f9888ba8bd654c4860ddfcb3a" PRIMARY KEY (company_id);
ALTER TABLE ONLY public.category
    ADD CONSTRAINT "PK_cc7f32b7ab33c70b9e715afae84" PRIMARY KEY (category_id);
ALTER TABLE ONLY public.emission_document
    ADD CONSTRAINT "PK_cdfc80463dd819a1a4a57f951e3" PRIMARY KEY (document_id);
ALTER TABLE ONLY public.emission_category_mapping
    ADD CONSTRAINT "PK_f0783bac5728ee347b1f5f5ed94" PRIMARY KEY (id);
ALTER TABLE ONLY public.user_sites
    ADD CONSTRAINT "PK_fb40cfe04628e6adefbe90c1eb4" PRIMARY KEY (user_id, site_id);
ALTER TABLE ONLY public.country
    ADD CONSTRAINT "UQ_2c5aa339240c0c3ae97fcc9dc4c" UNIQUE (name);
ALTER TABLE ONLY public.emission_threshold
    ADD CONSTRAINT "UQ_5fc7169ed8d2bb450dc6c432f7e" UNIQUE (company_id);
ALTER TABLE ONLY public.country
    ADD CONSTRAINT "UQ_8ff4c23dc9a3f3856555bd86186" UNIQUE (code);
ALTER TABLE ONLY public.emission_factors
    ADD CONSTRAINT "UQ_d0f5fda8a275586b0e00c440c06" UNIQUE (site_id, category_id, year, emission_category_name);
ALTER TABLE ONLY public.emission_category_mapping
    ADD CONSTRAINT "UQ_d92256334832af6af189c86b359" UNIQUE (company_id, site_id, category_id, company_category_name);
ALTER TABLE ONLY public."user"
    ADD CONSTRAINT "UQ_e12875dfb3b1d92d7d7c5377e22" UNIQUE (email);
CREATE INDEX "IDX_1ef071385f809b40c7657fb2ef" ON public.site_categories USING btree (category_id);
CREATE INDEX "IDX_30bcec7844d379c07fbe4ac8a8" ON public.emission USING btree (upload_batch_id);
CREATE INDEX "IDX_45435150cf9c026d714c9d847e" ON public.user_categories USING btree (user_id);
CREATE INDEX "IDX_7309e8a81a4f7383945b0b9475" ON public.column_config_columns USING btree (column_config_id);
CREATE INDEX "IDX_76a7e1a212a3784b96140b713b" ON public.user_sites USING btree (site_id);
CREATE INDEX "IDX_78d822ee1ebeb703dbecdf8e61" ON public.user_categories USING btree (category_id);
CREATE INDEX "IDX_85f257e9212fe12ca6307db4c3" ON public.notification USING btree (read);
CREATE INDEX "IDX_876af02663eefdbbd607c73634" ON public.user_sites USING btree (user_id);
CREATE INDEX "IDX_8bdc07e9c41ce8d83730f0f5d8" ON public.notification USING btree (created_at);
CREATE INDEX "IDX_bef355fa4ae83acba4ef70a833" ON public.site_categories USING btree (site_id);
CREATE INDEX "IDX_bfbd49560a7446ce9a2825cffb" ON public.column_config_columns USING btree (column_id);
ALTER TABLE ONLY public.production_data
    ADD CONSTRAINT "FK_0c2b630e80d4f211698357d82ea" FOREIGN KEY (product_id) REFERENCES public.product(product_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.site_categories
    ADD CONSTRAINT "FK_1ef071385f809b40c7657fb2ef1" FOREIGN KEY (category_id) REFERENCES public.category(category_id);
ALTER TABLE ONLY public.emission
    ADD CONSTRAINT "FK_25404473ff2a404581bb39af459" FOREIGN KEY (category_id) REFERENCES public.category(category_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.production_data
    ADD CONSTRAINT "FK_26f5d081d8326a0a0a9eba46444" FOREIGN KEY (reviewed_by) REFERENCES public."user"(user_id);
ALTER TABLE ONLY public.unit
    ADD CONSTRAINT "FK_4387e2d4aff1701078b83659d57" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_categories
    ADD CONSTRAINT "FK_45435150cf9c026d714c9d847e6" FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.column_config
    ADD CONSTRAINT "FK_568ce112512ad2f65ca246683d4" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.column_config
    ADD CONSTRAINT "FK_5dede17c26c99aa82914ef9a4ce" FOREIGN KEY (category_id) REFERENCES public.category(category_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.emission_threshold
    ADD CONSTRAINT "FK_5fc7169ed8d2bb450dc6c432f7e" FOREIGN KEY (company_id) REFERENCES public.company(company_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.production_data
    ADD CONSTRAINT "FK_6af3b33a3cf3e3d81dd543ade31" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.site
    ADD CONSTRAINT "FK_6d80331403939350918a8ddc3d3" FOREIGN KEY (country_id) REFERENCES public.country(country_id);
ALTER TABLE ONLY public.unit
    ADD CONSTRAINT "FK_712ecbc07fd22c595283395f4c9" FOREIGN KEY (category_id) REFERENCES public.category(category_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.emission_document
    ADD CONSTRAINT "FK_714b8d2a610024ff53b96a20cbb" FOREIGN KEY (emission_id) REFERENCES public.emission(pk_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.column_config_columns
    ADD CONSTRAINT "FK_7309e8a81a4f7383945b0b94758" FOREIGN KEY (column_config_id) REFERENCES public.column_config(pk_id) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.user_sites
    ADD CONSTRAINT "FK_76a7e1a212a3784b96140b713ba" FOREIGN KEY (site_id) REFERENCES public.site(site_id);
ALTER TABLE ONLY public.user_categories
    ADD CONSTRAINT "FK_78d822ee1ebeb703dbecdf8e616" FOREIGN KEY (category_id) REFERENCES public.category(category_id) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.emission
    ADD CONSTRAINT "FK_797c0f8d91474874e50b78d4363" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.production_data
    ADD CONSTRAINT "FK_874dc964de25b9103ac2325dbb8" FOREIGN KEY (created_by) REFERENCES public."user"(user_id);
ALTER TABLE ONLY public.user_sites
    ADD CONSTRAINT "FK_876af02663eefdbbd607c736346" FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public."user"
    ADD CONSTRAINT "FK_89798d5654ea24c7c42edf96f48" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.emission_factors
    ADD CONSTRAINT "FK_8b871cea02fde159c907e3db623" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.notification
    ADD CONSTRAINT "FK_928b7aa1754e08e1ed7052cb9d8" FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.product
    ADD CONSTRAINT "FK_a0f6258bbcc76e61da29f8c0ad0" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.site
    ADD CONSTRAINT "FK_a186b0cae821e5e9c6743dd9831" FOREIGN KEY (company_id) REFERENCES public.company(company_id);
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT "FK_aa306095b82fb1cc60fd31e2e60" FOREIGN KEY (changed_by) REFERENCES public."user"(user_id);
ALTER TABLE ONLY public.site_categories
    ADD CONSTRAINT "FK_bef355fa4ae83acba4ef70a833a" FOREIGN KEY (site_id) REFERENCES public.site(site_id) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.column_config_columns
    ADD CONSTRAINT "FK_bfbd49560a7446ce9a2825cffb2" FOREIGN KEY (column_id) REFERENCES public.column_entity(pk_id);
ALTER TABLE ONLY public.emission_document
    ADD CONSTRAINT "FK_d847c67bdb351d893fd3e92f68f" FOREIGN KEY (uploaded_by) REFERENCES public."user"(user_id);
ALTER TABLE ONLY public.emission_factors
    ADD CONSTRAINT "FK_eaa63edc8bc9ba068b588ad46ab" FOREIGN KEY (category_id) REFERENCES public.category(category_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.emission
    ADD CONSTRAINT "FK_f377df3d89418c8c5853a0d5c37" FOREIGN KEY (reviewed_by) REFERENCES public."user"(user_id);
ALTER TABLE ONLY public.emission
    ADD CONSTRAINT "FK_fbc0d494f4db3e17e779b2d6800" FOREIGN KEY (created_by) REFERENCES public."user"(user_id);
