db = db.getSiblingDB("qualora");

db.createCollection("_init");

db.createCollection("users");
db.users.createIndex({ "firebase_uid": 1 }, { unique: true });
db.users.createIndex({ "email": 1 });
db.users.createIndex({ "org_id": 1 });

db.createCollection("organizations");
db.organizations.createIndex({ "slug": 1 }, { unique: true });

db.createCollection("projects");
db.projects.createIndex({ "org_id": 1, "created_at": -1 });
db.projects.createIndex({ "org_id": 1, "name": 1 });

db.createCollection("test_cases");
db.test_cases.createIndex({ "org_id": 1, "status": 1 });
db.test_cases.createIndex({ "org_id": 1, "priority": 1 });
db.test_cases.createIndex({ "org_id": 1, "created_at": -1 });
db.test_cases.createIndex({ "org_id": 1, "tags": 1 });
db.test_cases.createIndex({ "org_id": 1, "project_id": 1, "status": 1 });

db.createCollection("test_runs");
db.test_runs.createIndex({ "org_id": 1, "created_at": -1 });
db.test_runs.createIndex({ "org_id": 1, "test_case_id": 1 });
db.test_runs.createIndex({ "org_id": 1, "status": 1 });

print("MongoDB initialization complete: 'qualora' database created with indexes.");
