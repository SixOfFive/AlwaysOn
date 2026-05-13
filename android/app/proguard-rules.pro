# Keep kotlinx-serialization metadata for our @Serializable classes
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt

-keep,includedescriptorclasses class com.sixoffive.ao.jarvis.**$$serializer { *; }
-keepclassmembers class com.sixoffive.ao.jarvis.** {
    *** Companion;
}
-keepclasseswithmembers class com.sixoffive.ao.jarvis.** {
    kotlinx.serialization.KSerializer serializer(...);
}
