
import json
import os
import boto3
import urllib.request
from decimal import Decimal

TABLE_NAME = os.environ.get("TABLE", "pp-integ-table")
OCI_ENDPOINT = os.environ.get("OCI_ENDPOINT")
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table(TABLE_NAME)

# Helper para convertir Decimals a float/int al serializar JSON
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super(DecimalEncoder, self).default(obj)

def onEvento(event, context):
    """
    Intercepta order.created y step.completed. 
    Actualiza DynamoDB y, si es de Rappi, notifica a OCI.
    """
    detail = event.get("detail", {})
    detail_type = event.get("detail-type")
    
    tenant_id = detail.get("tenantId")
    order_id = detail.get("orderId")
    
    if not tenant_id or not order_id:
        return {"ok": False, "msg": "Faltan llaves primarias"}

    # 1. Guardar o actualizar en DynamoDB
    if detail_type == "order.created":
        table.put_item(Item={
            "tenant_id": tenant_id,
            "order_id": order_id,
            "origin": detail.get("origin", "web"),
            "status": "RECIBIDO",
            "created_at": detail.get("createdAt")
        })
    
    elif detail_type == "step.completed":
        new_step = detail.get("step")

        # Recuperar el pedido para saber el origen
        response = table.get_item(Key={"tenant_id": tenant_id, "order_id": order_id})
        item = response.get("Item", {})
        origin = item.get("origin", "web")

        # Actualizar estado
        table.update_item(
            Key={"tenant_id": tenant_id, "order_id": order_id},
            UpdateExpression="SET #st = :s",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": new_step}
        )

        # 2. Sincronización Inter-Nube Bi-Direccional (Hacia OCI)
        if origin == "rappi":
            payload = json.dumps({
                "orderId": order_id,
                "status": new_step,
                "tenantId": tenant_id
            }).encode('utf-8')

            req = urllib.request.Request(OCI_ENDPOINT, data=payload, method='POST')
            req.add_header('Content-Type', 'application/json')

            try:
                urllib.request.urlopen(req, timeout=5)
                print(f"Notificación exitosa a OCI para orden {order_id}")
            except Exception as e:
                print(f"Error al notificar a OCI: {str(e)}")

    return {"ok": True}

def dashboardMetrics(event, context):
    """
    Endpoint GET /integ/metrics protegido por Cognito.
    """
    # 1. Extraer tenant_id seguro desde el JWT
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    tenant_id = claims.get("custom:tenant_id")
    
    if not tenant_id:
        return {"statusCode": 403, "body": json.dumps({"msg": "No autorizado"})}
    
    # 2. Consultar registros del Tenant
    response = table.query(
        KeyConditionExpression="tenant_id = :t",
        ExpressionAttributeValues={":t": tenant_id}
    )
    items = response.get("Items", [])
    
    # 3. Lógica de Agregación Analítica en Memoria
    totales = len(items)
    rappi = sum(1 for i in items if i.get("origin") == "rappi")
    web = totales - rappi
    
    por_estado = {
        "RECIBIDO": 0, "EN_COCINA": 0, "EN_DESPACHO": 0, "EN_REPARTO": 0, "ENTREGADO": 0
    }
    for i in items:
        st = i.get("status", "RECIBIDO")
        if st in por_estado:
            por_estado[st] += 1

    metrics = {
        "pedidosTotales": totales,
        "pedidosRappi": rappi,
        "pedidosWeb": web,
        "porEstado": por_estado
    }
    
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        },
        "body": json.dumps(metrics, cls=DecimalEncoder)
    }
