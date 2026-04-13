# EC2 Deployment Guide for COG Nodata Updater

## Quick Start

### Option 1: Deploy from Local Machine (Recommended)

```bash
# Deploy using SSH (Ubuntu instances)
./deploy_to_ec2.sh deploy ubuntu@<your-ec2-hostname> /path/to/your-key.pem

# Example with actual hostname:
./deploy_to_ec2.sh deploy ubuntu@ec2-35-91-143-44.us-west-2.compute.amazonaws.com kyle-disasters.pem

# For Amazon Linux instances, use ec2-user:
./deploy_to_ec2.sh deploy ec2-user@<your-ec2-ip> /path/to/your-key.pem
```

**Note:** The deployment script will:
1. Copy files to `~/cog-processing` on EC2
2. Install GDAL and system dependencies
3. Install Python pip (if missing)
4. Create a Python virtual environment
5. Install all required Python packages (rasterio, boto3, numpy, rio-cogeo)
6. Create a wrapper script for easy execution

### Option 2: Deploy via S3

```bash
# From your local machine, upload to S3
./deploy_to_ec2.sh deploy-s3 your-bucket-name

# Then on EC2 instance
mkdir -p ~/cog-processing && cd ~/cog-processing
aws s3 cp s3://your-bucket-name/scripts/cog-processing/update_nodata_cog_ec2.py .
aws s3 cp s3://your-bucket-name/scripts/cog-processing/deploy_to_ec2.sh .
chmod +x deploy_to_ec2.sh
./deploy_to_ec2.sh setup
```

### Option 3: Manual Setup on EC2

If you need to set up manually (e.g., files already copied):

```bash
# 1. Install system dependencies
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev python3-dev build-essential python3-pip python3-venv

# 2. Create virtual environment
cd ~/cog-processing
python3 -m venv venv
source venv/bin/activate

# 3. Install Python packages
pip install --upgrade pip
pip install rasterio boto3 numpy rio-cogeo

# 4. Create wrapper script
cat > run_cog_processor.sh << 'EOF'
#!/bin/bash
cd ~/cog-processing
source venv/bin/activate
python3 update_nodata_cog_ec2.py "$@"
EOF
chmod +x run_cog_processor.sh
```

## Python Virtual Environment (Important!)

**Ubuntu 24.04+ uses externally-managed Python**, which means you cannot install packages globally with pip. The deployment creates a virtual environment automatically.

The deployment includes a wrapper script `run_cog_processor.sh` that:
- Activates the virtual environment
- Runs the COG processor with your arguments
- Handles all the environment setup automatically

**Always use the wrapper script** instead of calling Python directly.

## Usage on EC2

Once deployed, you can use the script in several ways:

### Process Local Files

```bash
cd ~/cog-processing

# Single file
./run_cog_processor.sh input.tif

# Multiple files with pattern
./run_cog_processor.sh "*.tif" -j 4

# With output directory
./run_cog_processor.sh "*.tif" -o /path/to/output -j 4
```

### Process S3 Files

```bash
# Process from S3, save to S3 with 4 parallel jobs
./run_cog_processor.sh \
  s3://my-bucket/input/ \
  -o s3://my-bucket/output/ \
  -j 4

# Process single S3 file
./run_cog_processor.sh \
  s3://my-bucket/input/file.tif \
  -o s3://my-bucket/output/

# Process S3 files, save locally
./run_cog_processor.sh \
  s3://my-bucket/input/ \
  -o /mnt/data/output \
  -j 4
```

### Advanced Options

```bash
# Custom temp directory (useful for large files)
./run_cog_processor.sh input.tif --temp-dir /mnt/nvme/tmp

# Parallel processing with 8 jobs
./run_cog_processor.sh "*.tif" -j 8

# See all options
./run_cog_processor.sh --help
```

### Using the Virtual Environment Directly

If you need to run Python commands manually:

```bash
cd ~/cog-processing
source venv/bin/activate

# Now you can use python3 and pip directly
python3 update_nodata_cog_ec2.py input.tif
pip list
```

## Configuration

Edit `~/cog-processing/config.example.sh` to set your default S3 paths and options:

```bash
cp config.example.sh config.sh
nano config.sh  # Edit with your settings
source config.sh  # Load configuration

# Now use the variables
./run_cog_processor.sh \
  s3://$S3_INPUT_BUCKET/$S3_INPUT_PREFIX/ \
  -o s3://$S3_OUTPUT_BUCKET/$S3_OUTPUT_PREFIX/ \
  -j $PARALLEL_JOBS
```

## Setting Up as a Service (Optional)

For recurring/scheduled processing:

```bash
# Create service file template
./deploy_to_ec2.sh service

# Edit the service file with your paths and ensure it uses the wrapper script
nano ~/cog-processing/cog-processor.service

# Update the ExecStart line to use the wrapper:
# ExecStart=/home/ubuntu/cog-processing/run_cog_processor.sh s3://YOUR-BUCKET/input/ -o s3://YOUR-BUCKET/output/ -j 4

# Install the service
sudo cp ~/cog-processing/cog-processor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cog-processor
sudo systemctl start cog-processor

# Check status
sudo systemctl status cog-processor

# View logs
tail -f ~/cog-processing/logs/service.log
```

## IAM Permissions Required

Your EC2 instance needs an IAM role with S3 permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::your-bucket-name/*",
        "arn:aws:s3:::your-bucket-name"
      ]
    }
  ]
}
```

## EC2 Instance Recommendations

### Instance Type
- **Small files (<100MB)**: t3.medium or t3.large
- **Large files (>1GB)**: c5.xlarge or c5.2xlarge (compute optimized)
- **Many parallel jobs**: m5.2xlarge or m5.4xlarge (memory optimized)

### Operating System
- **Ubuntu 24.04 LTS** (tested and recommended)
- **Amazon Linux 2023** (also supported)
- **Ubuntu 22.04 LTS** (supported)

### Storage
- Use instance storage (ephemeral) for temp files when available
- Mount EBS volume with sufficient space for temporary processing
- Consider using EBS with provisioned IOPS for better performance

### Example mount temp storage:

```bash
# Check for instance store
lsblk

# Format and mount (if available)
sudo mkfs -t ext4 /dev/nvme1n1
sudo mkdir /mnt/nvme
sudo mount /dev/nvme1n1 /mnt/nvme
sudo chown ubuntu:ubuntu /mnt/nvme  # Use ubuntu:ubuntu for Ubuntu instances

# Use it for temp files
./run_cog_processor.sh input.tif --temp-dir /mnt/nvme/tmp
```

## Monitoring and Logs

All processing logs are saved to `~/cog-processing/` with timestamps in the filename.

```bash
# View latest log
ls -lt ~/cog-processing/*.log | head -n 1

# Tail the latest log
tail -f ~/cog-processing/nodata_update_*.log

# Search for errors
grep ERROR ~/cog-processing/*.log
```

## Troubleshooting

### Wrong username for SSH
```bash
# Ubuntu instances use 'ubuntu'
./deploy_to_ec2.sh deploy ubuntu@<hostname> key.pem

# Amazon Linux uses 'ec2-user'
./deploy_to_ec2.sh deploy ec2-user@<hostname> key.pem

# To test which user to use:
ssh -i key.pem ubuntu@<hostname> "whoami"
```

### Externally-managed Python environment error

If you see:
```
error: externally-managed-environment
× This environment is externally managed
```

**Solution:** The deployment automatically handles this by creating a virtual environment. Always use `./run_cog_processor.sh` instead of calling Python directly.

If you need to install additional packages:
```bash
cd ~/cog-processing
source venv/bin/activate
pip install <package-name>
```

### GDAL not found
```bash
# Ubuntu
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev

# Amazon Linux 2
sudo yum install -y gdal gdal-devel
```

### pip not found
```bash
# Ubuntu
sudo apt-get install -y python3-pip python3-venv

# Amazon Linux 2
sudo yum install -y python3-pip
```

### Permission denied on S3
- Check IAM role attached to EC2 instance
- Verify S3 bucket policies
- Test with: `aws s3 ls s3://your-bucket/`

### Out of memory
- Use a larger instance type
- Reduce parallel jobs: `-j 2` instead of `-j 4`
- Use instance storage for temp files

### Temp directory full
```bash
# Check disk space
df -h

# Clean temp files
rm -rf /tmp/*.tif

# Use a different temp directory
./run_cog_processor.sh input.tif --temp-dir /mnt/data/tmp
```

### Deployment stuck on apt-get
If deployment fails with "Unable to acquire the dpkg frontend lock":
- Wait 2-3 minutes for automatic updates to complete
- Then retry the deployment command

### Virtual environment activation fails
```bash
# Recreate the virtual environment
cd ~/cog-processing
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install rasterio boto3 numpy rio-cogeo
```

## Performance Tips

1. **Use parallel processing**: `-j 4` for 4 concurrent files
2. **Process files on the same region**: EC2 and S3 in same AWS region
3. **Use instance storage**: Faster than EBS for temp files
4. **Batch processing**: Process multiple files in one job
5. **Monitor costs**: Use spot instances for batch processing

## Complete Example Workflow

```bash
# 1. Deploy to EC2 (from your local machine)
cd ec2/
./deploy_to_ec2.sh deploy ubuntu@ec2-35-91-143-44.us-west-2.compute.amazonaws.com kyle-disasters.pem

# 2. SSH to EC2
ssh -i kyle-disasters.pem ubuntu@ec2-35-91-143-44.us-west-2.compute.amazonaws.com

# 3. Verify installation
cd ~/cog-processing
./run_cog_processor.sh --help

# 4. Process files from S3
./run_cog_processor.sh \
  s3://disasters-bucket/raw-geotiffs/ \
  -o s3://disasters-bucket/processed-cogs/ \
  -j 4 \
  --temp-dir /tmp

# 5. Monitor progress (in another terminal)
ssh -i kyle-disasters.pem ubuntu@ec2-35-91-143-44.us-west-2.compute.amazonaws.com
tail -f ~/cog-processing/nodata_update_*.log

# 6. Verify results
aws s3 ls s3://disasters-bucket/processed-cogs/
```

## What Gets Fixed

The COG processor automatically:
- Detects and remaps extreme nodata values (like `3.3999999521443642e+38`) to `-9999`
- Sets proper nodata values based on data type (0 for uint8, -9999 for float)
- Creates Cloud Optimized GeoTIFFs with:
  - ZSTD compression
  - 512x512 block size
  - 5 levels of overviews
  - Proper IFD ordering
  - Nearest neighbor resampling for overviews

## Supported File Sources

- ✅ Local files on EC2 instance
- ✅ S3 buckets (same or different region)
- ✅ Glob patterns (`*.tif`, `202405*.tif`, etc.)
- ✅ Individual files
- ✅ Directories

## Cost Optimization

1. **Use Spot Instances**: Save up to 90% for batch processing
2. **Auto-shutdown**: Configure EC2 to stop when idle
3. **S3 Lifecycle Policies**: Move processed files to cheaper storage tiers
4. **Right-size instances**: Start small, scale up if needed
5. **Same region**: Avoid inter-region data transfer costs

## Security Best Practices

1. **Never commit PEM keys**: Use `.gitignore` to exclude `*.pem` files
2. **Use IAM roles**: Attach IAM role to EC2 instead of storing AWS credentials
3. **Restrict Security Groups**: Only allow SSH from your IP
4. **Regular updates**: Keep system packages updated
5. **VPC configuration**: Use private subnets for production workloads
