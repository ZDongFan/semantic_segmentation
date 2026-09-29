CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS lcc_projects (
    id uuid PRIMARY KEY, name text NOT NULL,
    current_version uuid, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lcc_images (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES lcc_projects(id),
    name text NOT NULL, image_path text NOT NULL, dem_path text,
    draft_id uuid, UNIQUE(project_id, image_path)
);
CREATE TABLE IF NOT EXISTS lcc_drafts (
    id uuid PRIMARY KEY, image_id uuid NOT NULL REFERENCES lcc_images(id),
    file_path text NOT NULL, sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lcc_versions (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES lcc_projects(id),
    base_version uuid REFERENCES lcc_versions(id),
    idempotency_key uuid NOT NULL, request_hash text NOT NULL,
    file_path text NOT NULL, sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS lcc_version_images (
    version_id uuid NOT NULL REFERENCES lcc_versions(id),
    image_id uuid NOT NULL REFERENCES lcc_images(id),
    draft_id uuid NOT NULL REFERENCES lcc_drafts(id),
    PRIMARY KEY(version_id, image_id)
);
CREATE TABLE IF NOT EXISTS lcc_features (
    version_id uuid NOT NULL REFERENCES lcc_versions(id),
    image_id uuid NOT NULL REFERENCES lcc_images(id),
    feature_id uuid NOT NULL, properties jsonb NOT NULL,
    geom geometry(MultiPolygon,4326) NOT NULL,
    PRIMARY KEY(version_id, image_id, feature_id),
    CHECK(ST_IsValid(geom) AND NOT ST_IsEmpty(geom))
);
