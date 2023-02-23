from datetime import datetime
from pyspark.sql import SparkSession
from pyspark.sql.functions import from_json, to_json, col, lit, struct
from pyspark.sql.types import StructType, StructField, StringType, LongType


#Данные для подключения postgres local

postgresql_settings = {
    'user': 'jovyan',
    'password': 'jovyan',
    'url': 'jdbc:postgresql://localhost:5432/de',
    'driver': 'org.postgresql.Driver',
    'dbtable': 'public.subscribers_feedback',
}


kafka_security_options = {
    'kafka.security.protocol': 'SASL_SSL',
    'kafka.sasl.mechanism': 'SCRAM-SHA-512',
    'kafka.sasl.jaas.config': 'org.apache.kafka.common.security.scram.ScramLoginModule required username="kafka-admin" password="de-kafka-admin-2022";',
}

# метод для записи данных в 2 target: в PostgreSQL для фидбэков и в Kafka для триггеров
def foreach_batch_function(df):
    # сохраняем df в памяти, чтобы не создавать df заново перед отправкой в Kafka
    df.persist()

    # записываем df в PostgreSQL с полем feedback
    to_db = (df.withColumn('feedback', lit(None).cast(StringType())))
    # создаём df для отправки в Kafka. Сериализация в json.
    to_kafka = (df.select(to_json(struct(col('*'))).alias('value')) \
        .select('value'))

    to_db.write.format('jdbc').mode('append') \
    .options(**postgresql_settings).save()
    # отправляем сообщения в результирующий топик Kafka без поля feedback
    to_kafka.write \
        .format('kafka') \
        .option('kafka.bootstrap.servers', 'rc1b-2erh7b35n4j4v869.mdb.yandexcloud.net:9091') \
        .options(**kafka_security_options) \
        .option('topic', 'zhukov_out') \
        .option('truncate', False) \
        .save()
    # очищаем память от df
    df.unpersist()

# необходимые библиотеки для интеграции Spark с Kafka и PostgreSQL
spark_jars_packages = ",".join(
        [
            "org.apache.spark:spark-sql-kafka-0-10_2.12:3.3.0",
            "org.postgresql:postgresql:42.4.0",
        ]
    )

# создаём spark сессию с необходимыми библиотеками в spark_jars_packages для интеграции с Kafka и PostgreSQL
spark = SparkSession.builder \
    .appName("RestaurantSubscribeStreamingService") \
    .config("spark.sql.session.timeZone", "UTC") \
    .config("spark.jars.packages", spark_jars_packages) \
    .getOrCreate()

# читаем из топика Kafka сообщения с акциями от ресторанов 
restaurant_read_stream_df = spark.readStream \
    .format('kafka') \
    .options(**kafka_security_options) \
    .option('kafka.bootstrap.servers', 'rc1b-2erh7b35n4j4v869.mdb.yandexcloud.net:9091') \
    .option('subscribe', 'zhukov_in') \
    .load()
    

# определяем схему входного сообщения для json
incoming_message_schema = StructType([
        StructField('restaurant_id', StringType(), nullable=True),
        StructField('adv_campaign_id', StringType(), nullable=True),
        StructField('adv_campaign_content', StringType(), nullable=True),
        StructField('adv_campaign_owner', StringType(), nullable=True),
        StructField('adv_campaign_owner_contact', StringType(), nullable=True),
        StructField('adv_campaign_datetime_start', LongType(), nullable=True),
        StructField('adv_campaign_datetime_end', LongType(), nullable=True),
        StructField('datetime_created', LongType(), nullable=True)
    ])

# определяем текущее время в UTC в миллисекундах
current_timestamp_utc = int(round(datetime.utcnow().timestamp()))

# десериализуем из value сообщения json и фильтруем по времени старта и окончания акции
filtered_read_stream_df = (restaurant_read_stream_df
          .withColumn('value', col('value').cast(StringType()))
          .withColumn('data', from_json(col('value'), incoming_message_schema ))
          .selectExpr('data.*')
          .filter((lit(datetime.now())>= col('adv_campaign_datetime_start'))&(lit(datetime.now())<col('adv_campaign_datetime_end')))
          )

# вычитываем всех пользователей с подпиской на рестораны
subscribers_restaurant_df = (spark.read
                    .format('jdbc')
                    .option('url', 'jdbc:postgresql://rc1a-fswjkpli01zafgjm.mdb.yandexcloud.net:6432/de')
                    .option('driver', 'org.postgresql.Driver')
                    .option('dbtable', 'subscribers_restaurants')
                    .option('user', 'student')
                    .option('password', 'de-student')
                    .load()
                    .dropDuplicates(['client_id', 'restaurant_id']))

# джойним данные из сообщения Kafka с пользователями подписки по restaurant_id (uuid). Добавляем время создания события.
result_df = (filtered_read_stream_df
            .join(subscribers_restaurant_df, 'restaurant_id')
            .withColumn("trigger_datetime_created", lit(datetime.now()))
            .select(
            'restaurant_id',
            'adv_campaign_id',
            'adv_campaign_content',
            'adv_campaign_owner',
            'adv_campaign_owner_contact',
            'adv_campaign_datetime_start',
            'adv_campaign_datetime_end',
            'datetime_created',
            'client_id',
            'trigger_datetime_created'))

# запускаем стриминг
result_df.writeStream \
    .foreachBatch(foreach_batch_function) \
    .start() \
    .awaitTermination() 
