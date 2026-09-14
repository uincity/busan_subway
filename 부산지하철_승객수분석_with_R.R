getwd()
setwd("D:/python/python 특강/프로젝트")

#2019년 1월~3월 사이의 부산지하철 승,하차 정보를 가지고 이용객 TOP 10 역을 확인해보고
#지도위에 시각화 해본다

library(dplyr)
library(ggplot2)
library(ggmap)  ##google map visualization
#install.packages("plotly")
library(plotly)

#승하차인원 csv파일 읽기
data= read.csv(file='./RAWDATA/부산교통공사_시간대별_승하차인원_2019년1_7월.csv',header=T) 

#데이터 확인 
head(data, n = 5)
str(data)
summary(data)

#데이터프레임 컬럼명 변경
names(data)
#컬럼 일괄 변경 
names(data) = c( '역번호', '역명','년월일','구분', '1시', '2시', '3시', '4시', '5시','6시', '7시', '8시', '9시', '10시', '11시', '12시','13시', '14시', '15시', '16시', '17시', '18시','19시', '20시', '21시', '22시', '23시', '24시')
names(data)

#년월일 컬럼 date 타입 변경: factor -> Date
data$년월일 <- as.Date(data$년월일, format="%Y-%m-%d")


#공백제거 안된 역 확인 
data[data$역명 == "초  량",]
#역명전후공백제거
data$역명 <- gsub('\\s',"", data$역명)
data[data$역명 == "초량",]

head(data, n = 5)

#결측데이터 확인 : 다행이 없음
table(is.na(data))

#역별로 건수 체크 :3월까지 총 90일, 승/하차 구분으로 180개의 데이터가 역별로 존재해야함 
library(dplyr)
df_stcnt <-  data %>% group_by(역명) %>% summarise(n = n())
df_stcnt
df_stcnt[df_stcnt$n > 180,]

df_stcnt2 <-  data %>% group_by(역번호) %>% summarise(n = n())
df_stcnt2
df_stcnt2[df_stcnt2$n < 180,]
#401	미남역 데이터 존재 4건만 존재함
data[data$역번호 == 401,]

#역번호가 다르므로 그대로 분석 진행하기로 함 
#일별 합계 계산하여 컬럼 추가하기 
data.num <- data[,5:28] #5번째 컬럼부터 28번째 컬럼까지 합하여 새로운 데이터 프레임 만들기
#rowSums(data.num)

#합계 컬럼 추가 
data$합계 <- rowSums(data.num)
head(data)
str(data)

#필요한 정보로 데이터프레임 구성하기.
#시간대별 분석이 아니므로 일 합계 만 가져와서 처리하자 
data[1:5,c(3,1,2,4,29)]
df = data[,c(3,1,2,4,29)]
head(df)
tail(df)

#일자별 승/하차 의 평균을 1일 이용객수로 하자 
dfg <- df %>%
        group_by(년월일,역번호,역명) %>%
        summarize(이용객수 = mean(합계))
head(dfg)
str(dfg)


## boxplot
ggplot(data= dfg, mapping =aes(x=역명, y= 이용객수))+
  geom_boxplot() +
  coord_flip()

#3개월데이터로 역별 일평균 데이터프레임 생성
dfg2 <- dfg %>%
        group_by(역번호,역명) %>%
        summarize(이용객수 = round(mean(이용객수)))
head(dfg2)
str(dfg2)
dfg3 <- dfg2[order(-dfg2$이용객수),]
dfg3
head(dfg3,n= 20)


#1.6 역사정보에서 위도/경도 데이터 가져와서 병합하기(기준일자 : 2019.05.20.)
dinfo= read.csv(file='./RAWDATA/부산교통공사_도시철도역사정보_20190520.csv',header=T) 
head(dinfo)

#역번호 오류정정 : 2020 ->202 
dinfo[dinfo$역사명 == "중동역",]
#역명 변경 : 참조 : http://leoslife.com/archives/3971

dinfo[dinfo$역사명 == "중동역", "역번호"] = 202
#역번호 확인 
dinfo[dinfo$역사명 == "중동역",]

#결측데이터 확인 : 다행이 없음
table(is.na(dinfo))


str(dfg3)
str(dinfo) # 역번호 기준으로 합병하기 위해 데이터구조 확인 dinfo는 역번호가 num , dfg3는 int타입 


#역사정보에서 역위도와 역경도정보만 가져오기

dinfo[1:5,c('역번호','역위도','역경도')]
dinfo2 = dinfo[,c('역번호','역위도','역경도')]

head(dinfo2)
str(dinfo2)
#역번호를 int타입으로 변경
dinfo2$역번호 <- as.integer(dinfo2$역번호)
str(dinfo2)

#역정보와 이용객수를 merge하기 
df_m <-  merge(dfg3,dinfo2,by='역번호')
df_m
str(df_m)


#상위 30개를 새로운 데이터 프레임으로 저장
dfg4 = head(dfg3,n=30)
dfg4[,2:3]
dfg5 <- dfg4[,2:3]
dfg5

#시각화 : 상위부터 보이게 끔 x축을 reorder로 처리 필요 
ggplot(dfg5, aes(x=reorder(역명,-이용객수) ,y=이용객수)) + 
  geom_bar(stat="identity", fill="dark blue",width = 0.7) + 
  ggtitle("부산지하철이용객수 TOP 30") 


#상위 30개를 새로운 데이터 프레임으로 저장
head(df_m,n=30)
dfm_sort <- df_m[order(-df_m$이용객수),]
dfm_sort
dfm_top <- head(dfm_sort,n=30)
dfm_top

##google map에 시각화하기
register_google(key='AIzaSyByyJbCvs_4wroydh7L5umaqX7PfPgH4PA') # 부여받은 키 등록

cen <- c(mean(dfm_top$역경도), mean(dfm_top$역위도)) # map location

cen
gc <- data.frame(lon=dfm_top$역경도, lat=dfm_top$역위도) # create data frame
gc
gc$lon <- ifelse(gc$lon>180, -(360-gc$lon), gc$lon) # 경도를 넘는 경우 변환
gc

map <- get_googlemap(center=cen, # on the map                   
                     maptype="roadmap",                     
                     zoom=12,                     
                     marker=gc)

ggmap(map)+theme(axis.title.x=element_blank(), # x,y axis blank              
                 axis.text.x=element_blank(),                 
                 axis.ticks.x=element_blank(),                 
                 axis.title.y=element_blank(),                 
                 axis.text.y=element_blank(),                 
                 axis.ticks.y=element_blank())


map <- get_googlemap(center=cen,
                     maptype="roadmap",
                     zoom=12) # 지도 정보 담기 

gmap <- ggmap(map) # 데이터 입력

gmap+geom_point(data=dfm_top, # 산점도 표현
                aes(x=역경도,y=역위도,size=이용객수*10000), # 원의 크기를 이용객수로 표시
                shape = 21, colour = "black", fill = "red",
                alpha=0.5) # 불투명도 표시

  
