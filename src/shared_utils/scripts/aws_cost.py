import boto3
from datetime import datetime, timedelta
import json

def get_cost_breakdown():
    ce = boto3.client('ce', region_name='us-east-1')
    
    # Get costs for the last 7 days
    end = datetime.now().date()
    start = end - timedelta(days=7)
    
    # Get cost by service
    response = ce.get_cost_and_usage(
        TimePeriod={
            'Start': start.strftime('%Y-%m-%d'),
            'End': end.strftime('%Y-%m-%d')
        },
        Granularity='DAILY',
        Metrics=['UnblendedCost'],
        GroupBy=[
            {'Type': 'DIMENSION', 'Key': 'SERVICE'}
        ]
    )
    
    print(f"\nDaily costs by service (last 7 days):")
    print("=" * 60)
    
    for result in response['ResultsByTime']:
        date = result['TimePeriod']['Start']
        print(f"\nDate: {date}")
        print("-" * 40)
        
        total = 0
        for group in result['Groups']:
            service = group['Keys'][0]
            cost = float(group['Metrics']['UnblendedCost']['Amount'])
            
            if cost > 0.01:  # Only show services costing more than 1 cent
                print(f"{service:.<35} ${cost:.2f}")
                total += cost
        
        print(f"{'TOTAL':.<35} ${total:.2f}")
    
    # Get cost by usage type for expensive services
    print(f"\n\nDetailed breakdown for expensive services:")
    print("=" * 60)
    
    # Find the most expensive service
    expensive_services = {}
    for result in response['ResultsByTime']:
        for group in result['Groups']:
            service = group['Keys'][0]
            cost = float(group['Metrics']['UnblendedCost']['Amount'])
            if service not in expensive_services:
                expensive_services[service] = 0
            expensive_services[service] += cost
    
    # Get details for top 3 services
    top_services = sorted(expensive_services.items(), key=lambda x: x[1], reverse=True)[:3]
    
    for service, _ in top_services:
        if service == 'Tax':
            continue
            
        print(f"\n{service}:")
        print("-" * 40)
        
        usage_response = ce.get_cost_and_usage(
            TimePeriod={
                'Start': start.strftime('%Y-%m-%d'),
                'End': end.strftime('%Y-%m-%d')
            },
            Granularity='MONTHLY',
            Metrics=['UnblendedCost'],
            Filter={
                'Dimensions': {
                    'Key': 'SERVICE',
                    'Values': [service]
                }
            },
            GroupBy=[
                {'Type': 'DIMENSION', 'Key': 'USAGE_TYPE'}
            ]
        )
        
        for result in usage_response['ResultsByTime']:
            for group in result['Groups']:
                usage_type = group['Keys'][0]
                cost = float(group['Metrics']['UnblendedCost']['Amount'])
                if cost > 0.01:
                    print(f"  {usage_type:.<50} ${cost:.2f}")

if __name__ == "__main__":
    try:
        get_cost_breakdown()
    except Exception as e:
        print(f"Error: {e}")
        print("\nMake sure you have AWS credentials configured and ce:* permissions")