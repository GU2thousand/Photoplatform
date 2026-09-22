module "monitoring" {
  source                  = "./modules/monitoring"
  name                    = local.name
  region                  = var.region
  cluster_name            = module.ecs.cluster_name
  service_names           = module.ecs.service_names
  alb_arn_suffix          = module.alb.arn_suffix
  target_group_arn_suffix = module.alb.target_group_arn_suffix
  rds_identifier          = module.rds.identifier
  mq_broker_name          = module.mq.broker_name
  alarm_sns_topic_arn     = var.alarm_sns_topic_arn
}
