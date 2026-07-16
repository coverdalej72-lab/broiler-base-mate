import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

export const api = axios.create({ baseURL: API });

// Dashboard
export const fetchDashboard = () => api.get("/dashboard").then((r) => r.data);

// Farms
export const fetchFarms = () => api.get("/farms").then((r) => r.data);
export const createFarm = (data) => api.post("/farms", data).then((r) => r.data);
export const deleteFarm = (id) => api.delete(`/farms/${id}`).then((r) => r.data);

// Sheds
export const fetchSheds = (farmId) =>
  api.get("/sheds", { params: farmId ? { farm_id: farmId } : {} }).then((r) => r.data);
export const fetchShed = (id) => api.get(`/sheds/${id}`).then((r) => r.data);
export const createShed = (data) => api.post("/sheds", data).then((r) => r.data);
export const updateShed = (id, data) => api.patch(`/sheds/${id}`, data).then((r) => r.data);
export const deleteShed = (id) => api.delete(`/sheds/${id}`).then((r) => r.data);

// Batches
export const fetchBatches = (params = {}) => api.get("/batches", { params }).then((r) => r.data);
export const fetchBatch = (id) => api.get(`/batches/${id}`).then((r) => r.data);
export const createBatch = (data) => api.post("/batches", data).then((r) => r.data);
export const closeBatch = (id, data) => api.post(`/batches/${id}/close`, data).then((r) => r.data);
export const fetchBatchReadings = (id) => api.get(`/batches/${id}/readings`).then((r) => r.data);

// Readings
export const ingestReading = (data) => api.post("/readings", data).then((r) => r.data);
export const fetchShedReadings = (shedId, limit = 50) =>
  api.get(`/sheds/${shedId}/readings`, { params: { limit } }).then((r) => r.data);
export const fetchLatest = (shedId) => api.get(`/sheds/${shedId}/latest`).then((r) => r.data);

// Breeds
export const fetchBreeds = () => api.get("/breeds").then((r) => r.data);
export const fetchBreedCurve = (breed, metric) =>
  api.get(`/breeds/${encodeURIComponent(breed)}/curve`, { params: { metric } }).then((r) => r.data);

// Alerts
export const fetchAlerts = (unackOnly = false) =>
  api.get("/alerts", { params: { unack_only: unackOnly } }).then((r) => r.data);
export const ackAlert = (id) => api.post(`/alerts/${id}/ack`).then((r) => r.data);

// Decisions
export const fetchDecisions = (params = {}) => api.get("/decisions", { params }).then((r) => r.data);
export const approveDecision = (id, approve) =>
  api.post("/decisions/approve", { decision_id: id, approve }).then((r) => r.data);

// Policies
export const fetchPolicies = () => api.get("/policies").then((r) => r.data);
export const updatePolicy = (actionType, data) =>
  api.patch(`/policies/${actionType}`, data).then((r) => r.data);

// Mother Hen
export const motherHenAnalyze = (shedId) =>
  api.post(`/mother-hen/analyze/${shedId}`).then((r) => r.data);
