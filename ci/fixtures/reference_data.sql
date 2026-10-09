-- Fixed reference data for the calculation snapshot (same rows as ESG-lite
-- ci/golden/fixture.sql). Loaded into the throwaway CI database only.
INSERT INTO company (company_id, name, address, contact_person) VALUES
  (1, 'CI Steel Co', '1 Test Road', 'CI');
INSERT INTO site (site_id, name, address, contact_person, company_id) VALUES
  (1, 'CI Plant A', 'Plot 1', 'CI', 1),
  (2, 'CI Plant B', 'Plot 2', 'CI', 1);
INSERT INTO category (category_id, category_name, scope) VALUES
  (1, 'Stationary Combustion', 'Scope 1'),
  (2, 'Purchased Electricity', 'Scope 2'),
  (3, 'Use of Sold Products', 'Scope 3'),
  (4, 'Upstream Transportation and Distribution', 'Scope 3'),
  (5, 'Waste Generated in Operations', 'Scope 3');
INSERT INTO site_categories (site_id, category_id) VALUES
  (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (2, 1), (2, 2);
INSERT INTO "user" (user_id, name, email, password, role, site_id) VALUES
  (1, 'CI User', 'ci-user@example.invalid', 'x', 'User', 1),
  (2, 'CI Manager', 'ci-manager@example.invalid', 'x', 'Manager', NULL);
INSERT INTO user_sites (user_id, site_id) VALUES (2, 1), (2, 2);

-- factor_value is kgCO2e per denominator_unit; the engine divides by 1000.
INSERT INTO emission_factors
  (emission_factor_id, site_id, category_id, year, factor_value, denominator_unit, source, emission_category_name, global_category_name) VALUES
  (1, 1, 1, 2024, 2680.0000, 'litre', 'CI', 'Diesel', 'Diesel'),
  (2, 1, 1, 2024, 183.0000, 'kwh', 'CI', 'Natural Gas', 'Natural Gas'),
  (3, 1, 2, 2024, 708.0000, 'kwh', 'CI', 'Grid Electricity', 'Grid Electricity'),
  (4, 1, 2, 2023, 690.5000, 'kwh', 'CI', 'Grid Electricity', 'Grid Electricity'),
  (5, 1, 3, 2024, 0.2330, 'kwh', 'CI', 'India Grid', 'India Grid'),
  (6, 1, 4, 2024, 105.4000, 'tonne.km', 'CI', 'HGV Diesel [tonne.km]', 'HGV Diesel [tonne.km]'),
  (7, 1, 4, 2024, 250.1200, 'km', 'CI', 'Van Diesel [km]', 'Van Diesel [km]'),
  (8, 1, 5, 2024, 467.0100, 'tonne', 'CI', 'Landfill', 'Landfill'),
  (9, 2, 1, 2024, 2680.0000, 'litre', 'CI', 'Diesel', 'Diesel'),
  (10, 2, 2, 2024, 708.0000, 'kwh', 'CI', 'Grid Electricity', 'Grid Electricity');

INSERT INTO column_config (pk_id, config_name, site_id, category_id, calculation) VALUES
  (1, 'Use of Sold Products', 1, 3, '{
     "mode": "per_method",
     "method_column": "Calculation Method",
     "identity_columns": ["Product Name"],
     "methods": {
       "Direct energy": {"multiply": ["Units Sold", "Energy per use", "Lifetime uses"], "activity_unit": "kwh"},
       "Share of energy": {"multiply": ["Units Sold", "Energy per use", "Share in use"], "percent": ["Share in use"], "activity_unit": "kwh"}
     }
   }'::jsonb),
  (2, 'Upstream Transportation', 1, 4, '{
     "mode": "per_unit",
     "identity_columns": ["Shipment Ref"],
     "legacy_field": "Distance travelled",
     "methods": {
       "tonne.km": {"multiply": ["Weight", "Distance travelled"]},
       "km": {"multiply": ["Distance travelled"]}
     }
   }'::jsonb);

SELECT setval(pg_get_serial_sequence('company', 'company_id'), 100);
SELECT setval(pg_get_serial_sequence('site', 'site_id'), 100);
SELECT setval(pg_get_serial_sequence('category', 'category_id'), 100);
SELECT setval(pg_get_serial_sequence('"user"', 'user_id'), 100);
SELECT setval(pg_get_serial_sequence('emission_factors', 'emission_factor_id'), 100);
SELECT setval(pg_get_serial_sequence('column_config', 'pk_id'), 100);
