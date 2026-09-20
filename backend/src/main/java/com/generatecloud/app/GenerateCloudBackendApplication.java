package com.generatecloud.app;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableScheduling;

@EnableScheduling
@ConfigurationPropertiesScan
@SpringBootApplication
public class GenerateCloudBackendApplication {

	public static void main(String[] args) {
		SpringApplication.run(GenerateCloudBackendApplication.class, args);
	}

}
