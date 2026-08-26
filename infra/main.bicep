targetScope = 'resourceGroup'

@minLength(1)
param environmentName string = 'production'

@minLength(1)
param location string = resourceGroup().location

@minLength(1)
param cosmosDatabaseName string = 'anke_money_prod'

@minLength(1)
param cosmosEntitiesContainerName string = 'anke_entities'

@minLength(1)
param cosmosIdentitiesContainerName string = 'anke_identities'

@minLength(1)
param cosmosLeasesContainerName string = 'anke_sync_leases'

// Non-secret Clerk values are supplied after the Production Clerk instance is selected.
param clerkIssuer string = ''
param clerkJwksUrl string = ''
param clerkAudience string = ''

// Keep the recipient out of source control. A pre-created action group ID keeps
// notification routing attached on repeat deployments without storing the email.
param alertEmail string = ''
param alertActionGroupId string = ''

// The exact Flex Consumption hostname is returned after provisioning and is set afterwards.
param mcpAllowedHosts string = ''

var resourceToken = uniqueString(subscription().id, resourceGroup().id, location, environmentName)
var tags = {
  application: 'anke-money'
  environment: environmentName
  'data-classification': 'financial-sensitive'
  'managed-by': 'bicep'
}

// Resource names use the Azure MCP naming rule: az + short resource prefix + deterministic token.
var storageName = 'azs${resourceToken}'
var cosmosName = 'azc${resourceToken}'
var keyVaultName = 'azk${resourceToken}'
var logAnalyticsName = 'azl${resourceToken}'
var appInsightsName = 'azi${resourceToken}'
var identityName = 'azm${resourceToken}'
var planName = 'azp${resourceToken}'
var functionAppName = 'azf${resourceToken}'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  tags: tags
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'deploymentpackage'
  properties: {
    publicAccess: 'None'
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    IngestionMode: 'LogAnalytics'
    WorkspaceResourceId: logAnalytics.id
    RetentionInDays: 30
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'
  tags: tags
  properties: {
    databaseAccountOfferType: 'Standard'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    // Azure Services traffic is required because the Function App uses managed identity.
    ipRules: [
      {
        ipAddressOrRange: '0.0.0.0'
      }
    ]
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        tier: 'Continuous7Days'
      }
    }
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: cosmosDatabaseName
  properties: {
    resource: {
      id: cosmosDatabaseName
    }
    options: {
      autoscaleSettings: {
        maxThroughput: 1000
      }
    }
  }
}

resource cosmosEntities 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDatabase
  name: cosmosEntitiesContainerName
  properties: {
    resource: {
      id: cosmosEntitiesContainerName
      partitionKey: {
        paths: [
          '/householdId'
        ]
        kind: 'Hash'
      }
    }
  }
}

resource cosmosIdentities 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDatabase
  name: cosmosIdentitiesContainerName
  properties: {
    resource: {
      id: cosmosIdentitiesContainerName
      partitionKey: {
        paths: [
          '/uid'
        ]
        kind: 'Hash'
      }
    }
  }
}

resource cosmosLeases 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDatabase
  name: cosmosLeasesContainerName
  properties: {
    resource: {
      id: cosmosLeasesContainerName
      partitionKey: {
        paths: [
          '/id'
        ]
        kind: 'Hash'
      }
    }
  }
}

resource cosmosDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmos
  name: guid(cosmos.id, identity.id, 'Cosmos DB Built-in Data Contributor')
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}

resource functionPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
  tags: tags
}

resource storageBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'Storage Blob Data Owner')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'Storage Blob Data Contributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageQueueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'Storage Queue Data Contributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storageTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'Storage Table Data Contributor')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource monitoringMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, identity.id, 'Monitoring Metrics Publisher')
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource keyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, 'Key Vault Secrets Officer')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource functionApp 'Microsoft.Web/sites@2024-11-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  tags: union(tags, {
    'hidden-link: /app-insights-resource-id': appInsights.id
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: functionPlan.id
    httpsOnly: true
    keyVaultReferenceIdentity: identity.id
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}deploymentpackage'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 20
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      appSettings: [
        {
          name: 'AzureWebJobsStorage__blobServiceUri'
          value: storage.properties.primaryEndpoints.blob
        }
        {
          name: 'AzureWebJobsStorage__queueServiceUri'
          value: storage.properties.primaryEndpoints.queue
        }
        {
          name: 'AzureWebJobsStorage__tableServiceUri'
          value: storage.properties.primaryEndpoints.table
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__clientId'
          value: identity.properties.clientId
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'ANKE_ENVIRONMENT'
          value: 'prod'
        }
        {
          name: 'ANKE_COSMOS_ENDPOINT'
          value: cosmos.properties.documentEndpoint
        }
        {
          name: 'ANKE_COSMOS_DATABASE'
          value: cosmosDatabaseName
        }
        {
          name: 'ANKE_COSMOS_ENTITIES_CONTAINER'
          value: cosmosEntitiesContainerName
        }
        {
          name: 'ANKE_COSMOS_IDENTITIES_CONTAINER'
          value: cosmosIdentitiesContainerName
        }
        {
          name: 'ANKE_COSMOS_LEASES_CONTAINER'
          value: cosmosLeasesContainerName
        }
        {
          name: 'ANKE_COSMOS_EXPECTED_ACCOUNT_NAME'
          value: cosmosName
        }
        {
          name: 'ANKE_COSMOS_ALLOW_SMOKE_WRITE'
          value: 'false'
        }
        {
          name: 'ANKE_COSMOS_KEY'
          value: ''
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: identity.properties.clientId
        }
        {
          name: 'CLERK_JWKS_URL'
          value: clerkJwksUrl
        }
        {
          name: 'CLERK_ISSUER'
          value: clerkIssuer
        }
        {
          name: 'CLERK_AUDIENCE'
          value: clerkAudience
        }
        {
          name: 'CLERK_BACKEND_API_URL'
          value: 'https://api.clerk.com'
        }
        {
          name: 'CLERK_SECRET_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=clerk-secret-key)'
        }
        {
          name: 'ANKE_SESSION_SIGNING_SECRET'
          value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=anke-session-signing-secret)'
        }
        {
          name: 'ANKE_APNS_TEAM_ID'
          value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=apns-team-id)'
        }
        {
          name: 'ANKE_APNS_KEY_ID'
          value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=apns-key-id)'
        }
        {
          name: 'ANKE_APNS_PRIVATE_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=apns-private-key)'
        }
        {
          name: 'ANKE_APNS_TOPIC'
          value: 'app.ankemoney.ios'
        }
        {
          name: 'ANKE_SESSION_TTL_SECONDS'
          value: '2592000'
        }
        {
          name: 'ANKE_AGENT_REQUESTS_PER_MINUTE'
          value: '120'
        }
        {
          name: 'ANKE_AGENT_FAILED_AUTH_THRESHOLD'
          value: '5'
        }
        {
          name: 'ANKE_MCP_ALLOWED_HOSTS'
          value: mcpAllowedHosts
        }
        {
          name: 'ANKE_MCP_ALLOWED_ORIGINS'
          value: ''
        }
      ]
    }
  }
  dependsOn: [
    deploymentContainer
    cosmosDataContributor
    storageBlobOwner
    storageBlobContributor
    storageQueueContributor
    storageTableContributor
    monitoringMetricsPublisher
    keyVaultSecretsUser
  ]
}

resource functionDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: functionApp
  name: 'azd${resourceToken}'
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmail)) {
  name: 'azag${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'ankeprod'
    enabled: true
    emailReceivers: [
      {
        name: 'primary-maintainer'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

resource cpuSaturationAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'aza${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    description: 'Anke Money Production Flex Function App CPU saturation is elevated.'
    severity: 2
    enabled: true
    scopes: [
      functionApp.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'CpuPercentage'
          metricName: 'CpuPercentage'
          metricNamespace: 'Microsoft.Web/sites'
          operator: 'GreaterThan'
          threshold: 85
          timeAggregation: 'Average'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: !empty(alertEmail) ? [
      {
        actionGroupId: actionGroup.id
      }
    ] : !empty(alertActionGroupId) ? [
      {
        actionGroupId: alertActionGroupId
      }
    ] : []
  }
}

output functionAppName string = functionApp.name
output functionAppDefaultHostname string = functionApp.properties.defaultHostName
output functionAppUrl string = 'https://${functionApp.properties.defaultHostName}'
output cosmosAccountName string = cosmos.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabaseName string = cosmosDatabase.name
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output appInsightsName string = appInsights.name
output logAnalyticsName string = logAnalytics.name
output managedIdentityClientId string = identity.properties.clientId
