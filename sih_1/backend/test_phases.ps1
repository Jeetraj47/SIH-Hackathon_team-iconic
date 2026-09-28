$base = "http://localhost:5000/api"
$results = @()

function Test-API {
    param([string]$Name, [string]$Method, [string]$Uri, [string]$Body, [string]$Token, [string]$Expected)
    
    $params = @{
        Uri = $Uri
        Method = $Method
        ContentType = "application/json"
    }
    if ($Token) { $params.Headers = @{ Authorization = "Bearer $Token" } }
    if ($Body) { $params.Body = $Body }
    
    try {
        $resp = Invoke-RestMethod @params
        $status = if ($resp.success) { "PASS" } else { "FAIL" }
        Write-Host "[$status] $Name" -ForegroundColor $(if ($status -eq "PASS") { "Green" } else { "Red" })
        return $resp
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($Expected -eq "error" -and $code) {
            Write-Host "[PASS] $Name (expected error $code)" -ForegroundColor Green
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            return $reader.ReadToEnd() | ConvertFrom-Json
        }
        Write-Host "[FAIL] $Name - Error: $($_.Exception.Message)" -ForegroundColor Red
        return $null
    }
}

Write-Host "`n" -NoNewline
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  PHASE 1: AUTH TESTING" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# Register admin
$admin = Test-API -Name "Register Admin" -Method POST -Uri "$base/auth/register" -Body (@{
    name = "Admin User"
    email = "admin_test@sih.com"
    password = "admin123"
    role = "admin"
} | ConvertTo-Json)

$adminToken = $admin.data.token
Write-Host "  Admin Token: $($adminToken.Substring(0,20))..." -ForegroundColor DarkGray

# Register driver
$driver = Test-API -Name "Register Driver" -Method POST -Uri "$base/auth/register" -Body (@{
    name = "Test Driver"
    email = "driver_test@sih.com"
    password = "driver123"
    role = "driver"
    vehicleId = "MH12AB1234"
    phone = "9876543210"
} | ConvertTo-Json)

$driverToken = $driver.data.token
Write-Host "  Driver Token: $($driverToken.Substring(0,20))..." -ForegroundColor DarkGray

# Register official
$official = Test-API -Name "Register Official" -Method POST -Uri "$base/auth/register" -Body (@{
    name = "Road Inspector"
    email = "official_test@sih.com"
    password = "official123"
    role = "official"
    phone = "9876543211"
} | ConvertTo-Json)

$officialToken = $official.data.token

# Login
$login = Test-API -Name "Login Driver" -Method POST -Uri "$base/auth/login" -Body (@{
    email = "driver_test@sih.com"
    password = "driver123"
} | ConvertTo-Json)

# Get profile
$me = Test-API -Name "Get Profile (auth)" -Method GET -Uri "$base/auth/me" -Token $driverToken

# Test auth failure
$noAuth = Test-API -Name "No Auth Token (401)" -Method GET -Uri "$base/auth/me" -Expected "error"

Write-Host "`n" -NoNewline
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  PHASE 2: LOCATION TRACKING" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# Push location for driver (Mumbai area)
$loc1 = Test-API -Name "Update Location (driver)" -Method PUT -Uri "$base/location/update" -Token $driverToken -Body (@{
    longitude = 72.8777
    latitude = 19.0760
    speed = 65
    heading = 180
    accuracy = 5
    source = "gps"
} | ConvertTo-Json)

# Push location for official (Pune area)
$loc2 = Test-API -Name "Update Location (official)" -Method PUT -Uri "$base/location/update" -Token $officialToken -Body (@{
    longitude = 73.8567
    latitude = 18.5204
    speed = 0
    heading = 0
    accuracy = 10
    source = "gps"
} | ConvertTo-Json)

# Get own location
$myLoc = Test-API -Name "Get My Location" -Method GET -Uri "$base/location/me" -Token $driverToken

# Nearby search
$nearby = Test-API -Name "Nearby Users (10km)" -Method GET -Uri "$base/location/nearby?lng=72.8777&lat=19.0760&radius=10" -Token $driverToken

# Location history
$history = Test-API -Name "Location History" -Method GET -Uri "$base/location/history" -Token $driverToken

# All locations (admin only)
$all = Test-API -Name "All Locations (admin)" -Method GET -Uri "$base/location/all?online=true" -Token $adminToken

# All locations (driver - should fail 403)
$forbidden = Test-API -Name "All Locations (driver 403)" -Method GET -Uri "$base/location/all" -Token $driverToken -Expected "error"

Write-Host "`n" -NoNewline
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  PHASE 2: MESH NETWORK" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# Register mesh nodes
$node1 = Test-API -Name "Register Mesh Node (gateway)" -Method POST -Uri "$base/mesh/register" -Token $adminToken -Body (@{
    nodeId = "GW-NH48-001"
    name = "NH-48 Gateway KM 100"
    type = "gateway"
    longitude = 72.9000
    latitude = 19.1000
    highway = "NH-48"
} | ConvertTo-Json)

$nodeId1 = $node1.data.node._id

$node2 = Test-API -Name "Register Mesh Node (sensor)" -Method POST -Uri "$base/mesh/register" -Token $adminToken -Body (@{
    nodeId = "SN-NH48-002"
    name = "NH-48 Sensor KM 105"
    type = "sensor"
    longitude = 72.9100
    latitude = 19.1050
    highway = "NH-48"
} | ConvertTo-Json)

$nodeId2 = $node2.data.node._id

$node3 = Test-API -Name "Register Mesh Node (relay)" -Method POST -Uri "$base/mesh/register" -Token $officialToken -Body (@{
    nodeId = "RL-NH44-001"
    name = "NH-44 Relay KM 350"
    type = "relay"
    longitude = 78.4867
    latitude = 17.3850
    highway = "NH-44"
} | ConvertTo-Json)

# List nodes
$nodes = Test-API -Name "List All Mesh Nodes" -Method GET -Uri "$base/mesh/nodes" -Token $driverToken
Write-Host "  Total nodes: $($nodes.count)" -ForegroundColor DarkGray

# Filter by type
$sensors = Test-API -Name "Filter Nodes (sensor)" -Method GET -Uri "$base/mesh/nodes?type=sensor" -Token $driverToken

# Filter by highway
$nh48 = Test-API -Name "Filter Nodes (NH-48)" -Method GET -Uri "$base/mesh/nodes?highway=NH-48" -Token $driverToken

# Get single node
$single = Test-API -Name "Get Node Details" -Method GET -Uri "$base/mesh/nodes/$nodeId1" -Token $driverToken

# Heartbeat
$hb = Test-API -Name "Node Heartbeat" -Method PUT -Uri "$base/mesh/nodes/$nodeId1/heartbeat" -Token $adminToken -Body (@{
    signalStrength = -45
    batteryLevel = 92
} | ConvertTo-Json)

# Update node
$upd = Test-API -Name "Update Node Status" -Method PUT -Uri "$base/mesh/nodes/$nodeId2/heartbeat" -Token $adminToken -Body (@{
    signalStrength = -70
    batteryLevel = 55
} | ConvertTo-Json)

# Mesh stats
$stats = Test-API -Name "Mesh Stats (admin)" -Method GET -Uri "$base/mesh/stats" -Token $adminToken
Write-Host "  Total: $($stats.data.overview.total), Active: $($stats.data.overview.active)" -ForegroundColor DarkGray

# Nearby nodes
$nearNodes = Test-API -Name "Nearby Mesh Nodes" -Method GET -Uri "$base/mesh/nodes/nearby?lng=72.9000&lat=19.1000&radius=50" -Token $driverToken

# Driver cannot register node
$driverNode = Test-API -Name "Register Node (driver 403)" -Method POST -Uri "$base/mesh/register" -Token $driverToken -Body (@{
    nodeId = "FAIL-001"
    name = "Should Fail"
    type = "sensor"
    longitude = 72.9
    latitude = 19.1
    highway = "NH-48"
} | ConvertTo-Json) -Expected "error"

Write-Host "`n" -NoNewline
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  PHASE 3: INCIDENT REPORTING" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# Report incidents
$inc1 = Test-API -Name "Report Incident (pothole)" -Method POST -Uri "$base/incidents" -Token $driverToken -Body (@{
    title = "Large Pothole Near KM 120"
    description = "Deep pothole on left lane near NH-48 KM 120 marker. Multiple vehicles swerving to avoid."
    category = "pothole"
    severity = "medium"
    longitude = 72.8800
    latitude = 19.0800
    highway = "NH-48 KM 120"
    affectedLanes = 1
} | ConvertTo-Json)

$incId1 = $inc1.data.incident._id

$inc2 = Test-API -Name "Report Incident (critical accident)" -Method POST -Uri "$base/incidents" -Token $officialToken -Body (@{
    title = "Multi-vehicle accident blocking highway"
    description = "Three vehicles involved in collision. Two lanes completely blocked. Emergency services on the way."
    category = "accident"
    severity = "critical"
    longitude = 72.9200
    latitude = 19.1200
    highway = "NH-48 KM 130"
    affectedLanes = 2
    estimatedClearTime = "2026-09-08T02:00:00Z"
} | ConvertTo-Json)

$incId2 = $inc2.data.incident._id

$inc3 = Test-API -Name "Report Incident (landslide)" -Method POST -Uri "$base/incidents" -Token $driverToken -Body (@{
    title = "Landslide debris on road surface"
    description = "Small landslide near Lonavala ghat section. Rocks and mud on roadway. Passable with caution."
    category = "landslide"
    severity = "high"
    longitude = 73.4000
    latitude = 18.7500
    highway = "NH-48 KM 95"
    affectedLanes = 1
} | ConvertTo-Json)

$incId3 = $inc3.data.incident._id

# Report using auto-location (from Phase 2 GPS)
$inc4 = Test-API -Name "Report Incident (auto-location)" -Method POST -Uri "$base/incidents" -Token $driverToken -Body (@{
    title = "Traffic signal malfunction at junction"
    description = "Traffic signal stuck on red for all directions at major junction. Traffic jam forming."
    category = "signal_failure"
    severity = "medium"
} | ConvertTo-Json)

# List all incidents
$incidents = Test-API -Name "List All Incidents" -Method GET -Uri "$base/incidents" -Token $driverToken
Write-Host "  Total incidents: $($incidents.total)" -ForegroundColor DarkGray

# Filter by category
$potholes = Test-API -Name "Filter (category=pothole)" -Method GET -Uri "$base/incidents?category=pothole" -Token $driverToken

# Filter by severity
$criticals = Test-API -Name "Filter (severity=critical)" -Method GET -Uri "$base/incidents?severity=critical" -Token $driverToken

# Filter by highway
$hw48 = Test-API -Name "Filter (highway=NH-48)" -Method GET -Uri "$base/incidents?highway=NH-48" -Token $driverToken

# Get single incident
$single = Test-API -Name "Get Incident Details" -Method GET -Uri "$base/incidents/$incId1" -Token $driverToken

# Nearby incidents
$nearInc = Test-API -Name "Nearby Incidents (50km)" -Method GET -Uri "$base/incidents/nearby?lng=72.9000&lat=19.1000&radius=50" -Token $driverToken
Write-Host "  Found nearby: $($nearInc.count)" -ForegroundColor DarkGray

# Verify incident (official)
$verify = Test-API -Name "Verify Incident (official)" -Method PUT -Uri "$base/incidents/$incId1/verify" -Token $officialToken
Write-Host "  Status after verify: $($verify.data.incident.status)" -ForegroundColor DarkGray

# Update incident (admin)
$update = Test-API -Name "Update Incident (admin)" -Method PUT -Uri "$base/incidents/$incId2" -Token $adminToken -Body (@{
    status = "in_progress"
    affectedLanes = 3
} | ConvertTo-Json)
Write-Host "  Status: $($update.data.incident.status)" -ForegroundColor DarkGray

# Resolve incident
$resolve = Test-API -Name "Resolve Incident" -Method PUT -Uri "$base/incidents/$incId1/resolve" -Token $adminToken
Write-Host "  Status: $($resolve.data.incident.status), Resolved: $($resolve.data.incident.resolvedAt)" -ForegroundColor DarkGray

# Incident stats
$incStats = Test-API -Name "Incident Stats (admin)" -Method GET -Uri "$base/incidents/stats" -Token $adminToken
Write-Host "  Overview: Total=$($incStats.data.overview.total), Reported=$($incStats.data.overview.reported), Verified=$($incStats.data.overview.verified), InProgress=$($incStats.data.overview.inProgress), Resolved=$($incStats.data.overview.resolved)" -ForegroundColor DarkGray

# Validation tests
Write-Host "`n" -NoNewline
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  VALIDATION TESTS" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

$badInc = Test-API -Name "Invalid Incident (short title)" -Method POST -Uri "$base/incidents" -Token $driverToken -Body (@{
    title = "Hi"
    description = "too short"
    category = "pothole"
    severity = "low"
} | ConvertTo-Json) -Expected "error"

$badLoc = Test-API -Name "Invalid Location (bad coords)" -Method PUT -Uri "$base/location/update" -Token $driverToken -Body (@{
    longitude = 999
    latitude = 999
} | ConvertTo-Json) -Expected "error"

$badNode = Test-API -Name "Duplicate Node ID" -Method POST -Uri "$base/mesh/register" -Token $adminToken -Body (@{
    nodeId = "GW-NH48-001"
    name = "Duplicate"
    type = "gateway"
    longitude = 72.9
    latitude = 19.1
    highway = "NH-48"
} | ConvertTo-Json) -Expected "error"

Write-Host "`n" -NoNewline
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  CLEANUP" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# Delete an incident (admin only)
$del = Test-API -Name "Delete Incident (admin)" -Method DELETE -Uri "$base/incidents/$incId3" -Token $adminToken

# Delete mesh node (admin only)
$delNode = Test-API -Name "Delete Mesh Node (admin)" -Method DELETE -Uri "$base/mesh/nodes/$nodeId2" -Token $adminToken

# Driver can't delete
$delFail = Test-API -Name "Delete Incident (driver 403)" -Method DELETE -Uri "$base/incidents/$incId2" -Token $driverToken -Expected "error"

Write-Host "`n" -NoNewline
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  ALL PHASE 2 + PHASE 3 TESTS COMPLETE!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host ""
